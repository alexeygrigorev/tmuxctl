.PHONY: test test-integration package-smoke publish-build publish-clean release

# Default suite: fast, no side effects. Excludes tests_integration/ via the
# pyproject testpaths setting, so it never spawns the memory-hog OOM test.
test:
	uv run pytest

# Integration suite: real systemd --user cgroup OOM isolation. Spawns a
# process that allocates until it is OOM-killed, so it is kept OUT of the
# default run and exercised on CI only (it self-skips where user scopes are
# unavailable). Run locally only if you know what you are doing.
test-integration:
	uv run pytest tests_integration

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
