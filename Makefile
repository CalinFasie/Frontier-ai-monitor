.PHONY: install test collect run

install:
	python -m pip install -r requirements.txt

test:
	python -m unittest discover -s tests -v

collect:
	python -m frontier_monitor.main --collect-only

run:
	python -m frontier_monitor.main
