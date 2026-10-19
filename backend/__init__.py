"""The server, split out of main.py one section at a time.

main.py is still the FastAPI app and the composition root: it imports from this
package and re-exports every name, so `main.<name>` and every caller left in
main.py are unchanged. Modules here never import main. The rules, and how a
move is verified, are in docs/BACKEND.md § Splitting main.py.
"""
