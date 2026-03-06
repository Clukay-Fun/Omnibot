# Release Checklist

Use this checklist before tagging or publishing a new release.

## 1) Code and quality gates

- Ensure CI is green for the target commit.
- Run local checks:
  - `ruff check .`
  - `pytest`
  - `python -m build --sdist --wheel`
- Confirm no unintended local changes are included.

## 2) Version and changelog

- Bump `version` in `pyproject.toml`.
- Summarize user-facing changes in release notes:
  - New features
  - Behavior changes
  - Fixes
  - Breaking changes (if any)

## 3) Config and security checks

- Verify no secrets are committed (tokens, keys, private URLs).
- Confirm `.env` and deployment secrets are configured in target environment.
- Re-check `SECURITY.md` guidance for disclosure and supported versions.

## 4) Packaging and smoke validation

- Build artifacts locally:
  - `python -m build --sdist --wheel`
- Optional local smoke test (recommended):
  - Create a clean venv.
  - Install wheel from `dist/`.
  - Run `nanobot --help`.

## 5) Publish and verify

- Create annotated git tag (for example `v0.1.5`).
- Push branch and tag.
- Tag push triggers `.github/workflows/release.yml` to build artifacts, run `twine check`, and create a GitHub Release.
- Optional: set `PYPI_API_TOKEN` repository secret to enable automatic PyPI publish in the same workflow.
- Verify release notes and downloadable artifacts on the release page.

## 6) Post-release checks

- Perform a basic runtime check in the target environment.
- Monitor logs for startup/runtime errors after deployment.
- Prepare a rollback plan if critical issues appear.
