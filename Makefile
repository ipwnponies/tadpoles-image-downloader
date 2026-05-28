.PHONY: install lint format typecheck

install:
	poetry install

lint:
	poetry run ruff check .

format:
	poetry run ruff format .

typecheck:
	poetry run mypy tadpoles_image_downloader/
