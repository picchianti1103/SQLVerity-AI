# Release process

The repository contains separate workflows for Python distributions and the multi-platform container
image. Both build from a GitHub release tag; only the Python workflow also supports a manual build-only
or TestPyPI run.

## One-time owner setup

These account-level operations must be completed by a repository owner before the first publication.

### PyPI and TestPyPI

1. Create accounts with two-factor authentication on PyPI and TestPyPI.
2. Configure a pending Trusted Publisher for project `sqlverity-platform` on each index.
3. Use owner `picchianti1103`, repository `SQLVerity-AI`, workflow
   `.github/workflows/publish-python.yml`, and environment `pypi` or `testpypi` respectively.
4. In the GitHub repository, create matching `pypi` and `testpypi` environments. Add required reviewers
   if the repository plan supports them. Do not create long-lived API-token secrets.

The workflow uses GitHub OIDC and the official PyPA publish action. A prerelease is routed to TestPyPI;
a non-prerelease GitHub release is routed to PyPI. A manual run can build only or target TestPyPI, but
cannot publish to production PyPI.

### GitHub Container Registry

No registry password is needed. The container workflow uses the release job's short-lived
`GITHUB_TOKEN`, scoped to `packages: write`. After the first successful publication, verify that the
package is public and connected to this repository in the package settings.

## Release checklist

1. Choose a semantic version and update `project.version` in `pyproject.toml`.
2. Move the relevant entries in `CHANGELOG.md` into that version.
3. Run the complete verification gate from `CONTRIBUTING.md`.
4. Merge the release preparation through a pull request.
5. Create a Git tag named exactly `v<project.version>` from the verified main commit.
6. Publish a GitHub prerelease for a TestPyPI/container candidate, or a normal release for PyPI and the
   stable container tags.
7. Approve the protected GitHub environment when prompted.
8. Verify the package and container provenance before announcing the release.

The Python workflow fails closed if the release tag and `pyproject.toml` version differ. Existing index
versions cannot be overwritten, so a failed release must be corrected with a new version.

## Local artifact check

Before creating a tag:

```powershell
python -m build
python -m twine check dist/*
```

This validates the package layout and metadata but does not upload anything.
