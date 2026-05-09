.PHONY: setup run clean

setup:
	python3 setup.py

run:
	python3 run.py

clean:
	rm -rf .venv __pycache__ .env
	find . -name "*.pyc" -delete
