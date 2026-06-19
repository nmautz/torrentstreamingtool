"""Windows ProactorEventLoop accept-loop hardening.

WHY THIS EXISTS
---------------
On Windows the asyncio default loop is the ``ProactorEventLoop`` (uvicorn
cannot use uvloop there, and we depend on the Proactor loop for
``asyncio.create_subprocess_exec`` — ffmpeg, whisper, git — so switching to the
SelectorEventLoop is *not* an option). The Proactor loop has a long-standing
CPython bug in ``BaseProactorEventLoop._start_serving``:

    def loop(f=None):
        try:
            if f is not None:
                conn, addr = f.result()      # <-- per-connection accept result
                ...make transport...
            f = self._proactor.accept(sock)
        except OSError as exc:
            if sock.fileno() != -1:
                self.call_exception_handler({'message': 'Accept failed ...'})
                sock.close()                 # <-- closes the LISTENING socket!
        ...

When a client vanishes between ``AcceptEx`` completing and us reading the
result (a port scan, a half-open TCP probe, a flaky Wi-Fi/VPN client, a NAT
that drops the flow), ``f.result()`` raises ``OSError`` — typically
``[WinError 64] The specified network name is no longer available`` (also 121,
1236, ECONNRESET, ECONNABORTED). That per-*connection* failure falls straight
into the ``except OSError`` branch, which closes the **listening** socket. From
that moment the server accepts no new connections: the dashboard hangs for
every client and only a process restart / reboot brings it back. We saw exactly
this in the field — "Accept failed on a socket" on ``laddr=('0.0.0.0', 80)``
followed by total unresponsiveness until reboot.

THE FIX
-------
Reinstall ``_start_serving`` with an inner ``loop`` that distinguishes a
per-connection accept failure (recoverable — log it and re-arm a fresh accept,
keeping the listener alive) from a genuine failure of the listening socket
itself (close, as upstream does). This mirrors how the SelectorEventLoop already
tolerates transient ``accept()`` errors.

This is Windows-first (see CLAUDE.md). The patch is a no-op on Linux/macOS,
where the SelectorEventLoop is used and this code path doesn't exist.
"""
from __future__ import annotations

import sys

_applied = False


def apply() -> bool:
    """Monkeypatch the Proactor accept loop. Idempotent; safe to call anywhere.

    Returns True if the patch is installed (or already was), False if it was
    skipped (non-Windows) or could not be applied (logged, then ignored).
    """
    global _applied
    if _applied:
        return True
    if sys.platform != "win32":
        return False

    try:
        from asyncio import proactor_events, trsock
        from asyncio import exceptions as _aio_exc

        BaseProactorEventLoop = proactor_events.BaseProactorEventLoop
        logger = proactor_events.logger
        TransportSocket = trsock.TransportSocket
        CancelledError = _aio_exc.CancelledError

        # Signature must match the running CPython. 3.13 added
        # ``ssl_shutdown_timeout``; older versions don't have it. We accept it
        # via **ssl_kw and forward only what _make_ssl_transport understands,
        # so this single definition works across 3.9–3.13+.
        def _start_serving(self, protocol_factory, sock,
                           sslcontext=None, server=None, backlog=100,
                           ssl_handshake_timeout=None, **ssl_kw):

            def loop(f=None):
                try:
                    if f is not None:
                        try:
                            conn, addr = f.result()
                        except OSError as exc:
                            # PER-CONNECTION accept failure: the peer went away
                            # mid-handshake (WinError 64/121/1236, ECONNRESET,
                            # ...). The LISTENING socket is still fine. Upstream
                            # CPython closes it here, permanently killing the
                            # server; we instead log and fall through to re-arm
                            # a fresh accept so the listener stays alive.
                            if sock.fileno() != -1:
                                self.call_exception_handler({
                                    'message': 'Accept failed on a socket '
                                               '(recovered, listener kept alive)',
                                    'exception': exc,
                                    'socket': TransportSocket(sock),
                                })
                            else:
                                # Listening socket was torn down concurrently.
                                return
                        else:
                            if self._debug:
                                logger.debug(
                                    "%r got a new connection from %r: %r",
                                    server, addr, conn)
                            protocol = protocol_factory()
                            if sslcontext is not None:
                                self._make_ssl_transport(
                                    conn, protocol, sslcontext,
                                    server_side=True,
                                    extra={'peername': addr}, server=server,
                                    ssl_handshake_timeout=ssl_handshake_timeout,
                                    **ssl_kw)
                            else:
                                self._make_socket_transport(
                                    conn, protocol,
                                    extra={'peername': addr}, server=server)
                    if self.is_closed():
                        return
                    f = self._proactor.accept(sock)
                except OSError as exc:
                    # Reaching here means arming a NEW accept on the listening
                    # socket failed (or making the transport did). That is a
                    # genuine listener-level error — close it, as upstream does.
                    if sock.fileno() != -1:
                        self.call_exception_handler({
                            'message': 'Accept failed on a socket',
                            'exception': exc,
                            'socket': TransportSocket(sock),
                        })
                        sock.close()
                    elif self._debug:
                        logger.debug("Accept failed on socket %r",
                                     sock, exc_info=True)
                except CancelledError:
                    sock.close()
                else:
                    self._accept_futures[sock.fileno()] = f
                    f.add_done_callback(loop)

            self.call_soon(loop)

        BaseProactorEventLoop._start_serving = _start_serving
        _applied = True
        return True
    except Exception as exc:  # pragma: no cover - defensive
        # Never let a patch failure prevent startup; fall back to stock asyncio.
        try:
            import logging
            logging.getLogger("streamlink").warning(
                "winaccept_patch: could not harden Proactor accept loop (%s: %s); "
                "running with stock asyncio behaviour", type(exc).__name__, exc)
        except Exception:
            pass
        return False
