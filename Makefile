.PHONY: test publish-build publish-test publish publish-clean

test:
	uv run pytest

publish-build:
	uv run hatch build

publish-test:
	uv run hatch publish --repo test

publish:
	uv run hatch publish

publish-clean:
	rm -rf dist build *.egg-info tmuxctl.egg-info
