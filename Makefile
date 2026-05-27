.PHONY: test package-smoke publish-build publish-clean release

test:
	uv run pytest

package-smoke:
	rm -rf .venv-smoke dist
	uv build
	python -m venv .venv-smoke
	.venv-smoke/bin/python -m pip install --upgrade pip
	.venv-smoke/bin/python -m pip install dist/*.whl
	.venv-smoke/bin/t --help >/dev/null

publish-build:
	uv run hatch build

publish-clean:
	rm -rf dist build *.egg-info tmuxctl.egg-info

# Release: tag the current version and push to trigger CI publish.
# CI workflow: .github/workflows/publish.yml (on tag push v*)
release:
	@VERSION=$$(grep '^version' pyproject.toml | head -1 | sed -E 's/.*"([^"]+)".*/\1/'); \
	echo "Releasing v$$VERSION"; \
	git tag "v$$VERSION"; \
	git push origin "v$$VERSION"
