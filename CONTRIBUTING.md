# Contributing

Cooler btop targets Linux systems and supports Python 3.9 and newer. Changes
should be small, reviewable, and reproducible from a clean checkout.

## Branch and commit workflow

Keep `main` releasable. Start work from an up-to-date local branch:

```bash
git fetch origin
git switch main
git pull --ff-only origin main
git switch -c fix/short-description
```

Use one branch for one coherent change. Use a short imperative commit subject,
for example `fix: handle missing memory fields`. Do not commit generated
directories such as `build/`, `dist/`, virtual environments, caches, logs, or
local databases.

Push a feature branch and open a pull request:

```bash
git push --set-upstream origin HEAD
```

Before updating a pull request, rebase the feature branch on the current
`origin/main` and push it with lease protection:

```bash
git fetch origin
git rebase origin/main
git push --force-with-lease
```

Never force-push `main` or a published release tag.

## Local verification

Run the checks relevant to the change before opening or updating a pull
request:

```bash
make test
make coverage
make distributions
make reproducibility-check
make wheel-smoke
COOLER_BTOP_STRESS_DURATION=5 python stress_test.py
git diff --check
```

On Fedora 44 with RPM tooling installed, also run the complete release check
with a temporary output directory and verify that directory’s checksum
manifest:

```bash
release_dir=$(mktemp -d)
make release DIST_DIR="$release_dir"
(cd "$release_dir" && sha256sum --check SHA256SUMS)
```

The GitHub workflows repeat the package and release checks in clean
environments.

## Pull requests

Pull requests should explain the user-visible change, include the commands
used for verification, and call out compatibility or security impact. Keep
unrelated formatting and generated files out of the change. A pull request
must have passing CI and an approving review before it is merged to `main`.

## Releases

1. Update the version in `pyproject.toml`, the RPM spec, the man page,
   AppStream metadata, README examples, and the matching changelog entry.
2. Merge the change through a reviewed pull request and confirm CI is green.
3. From the resulting `main` commit, create and verify a signed annotated tag:

   ```bash
   git switch main
   git pull --ff-only origin main
   git tag -s vX.Y.Z -m "Cooler btop X.Y.Z"
   git tag -v vX.Y.Z
   git push origin vX.Y.Z
   ```

4. Confirm the tag matches the project version. The release workflow builds
   the RPM, source RPM, wheel, source archive, and checksum manifest, creates
   a signed GitHub artifact attestation from that manifest, then publishes only
   those verified artifacts. A downloaded release artifact can be checked with
   `gh attestation verify ARTIFACT --repo l0ee/cooler_btop`.

If a published commit is wrong, use a reviewed `git revert` on `main` and
publish a new patch release when needed. Do not rewrite public branch or tag
history to repair a release.

## Maintainer settings

Repository administrators should protect `main` with pull-request reviews,
required CI status checks, stale-review dismissal, and force-push/deletion
disabled. Require signed commits or otherwise verify the release tag. Keep
third-party GitHub Actions pinned to full commit SHAs and update those pins by
reviewed pull request.

The daemon binds to loopback by default. Any deployment that widens its bind
address must provide access control and transport security outside this
repository.
