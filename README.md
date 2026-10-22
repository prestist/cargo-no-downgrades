# cargo-no-downgrades

## Temporary CoreOS fork

This fork carries fixes for
[Garfield1002/cargo-no-downgrades#1](https://github.com/Garfield1002/cargo-no-downgrades/issues/1):
dependency switches hidden by multiple locked versions, and SemVer prerelease
ordering. It is intended for temporary use by
[coreos/repo-templates#375](https://github.com/coreos/repo-templates/pull/375).
Return to a full-SHA-pinned upstream revision once equivalent fixes and tests
are available there. Fork consumers should pin a **published commit containing
these fixes**, not the inherited upstream `v1` tag or `5adc12f` revision.

## Usage

Fails a pull request whose `Cargo.lock` pins any package below the version
the base branch locks.

The usual way a downgrade reaches `main`: a branch is cut, `main` bumps a
dependency, the branch is merged with its older pin and lock entry intact.
Cargo does not object, because the older version still satisfies the
branch's requirement.

```yaml
jobs:
  no-downgrades:
    if: github.event_name == 'pull_request'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      # Replace this placeholder with the full SHA of a published fix commit.
      - uses: prestist/cargo-no-downgrades@<full-fixed-commit-sha>
```

Inputs: `base` (defaults to the pull request's base branch) and `lockfile`
(defaults to `Cargo.lock`). Only packages the base already locks are
compared, by name and source. The highest locked versions are compared as in
upstream, and dependency edges are checked too: changing `app -> rand 0.9.5`
to `app -> rand 0.8.8` fails even if both rand versions remain in the lockfile.
Pre-releases use SemVer identifier ordering (`alpha.2 < alpha.10`) and sort
below their release; build metadata is ignored.
It needs Python 3.11 or newer (`tomllib`) on the runner and no third-party packages.

### Matching and limitations

- Dependency IDs are resolved independently in each lockfile, including omitted
  versions, explicit registry/Git sources, and source-less path dependencies.
- Parent packages match by name and source, then by identical version. If exactly
  one old and one new parent version remain, those are compared too, so a release
  version bump does not hide a downgrade. Ambiguous multi-version parent changes
  are not paired arbitrarily.
- For each matched parent and dependency name/source, unchanged dependency
  versions are removed from consideration. Equal-sized sets of removed/added
  versions are compared in SemVer order. Pure edge additions/removals and unequal
  replacement counts are not inferred to be downgrades. The original global
  highest-version check still applies, including when a highest version is removed.
- Different package sources (including different Git commit hashes) are not
  compared. This is a version check, not a Git ancestry check. Lockfiles do not
  encode dependency aliases, kinds, targets, or features, so this cannot detect
  every possible dependency-graph change.
- Invalid or ambiguous dependency IDs fail the check rather than silently
  skipping an edge. No Cargo commands or code from the checked project run.

## Tests

Run `python3 -B -m unittest -v` (standard library only). The suite includes a
minimized regression from coreos-installer #1772, where the package version
changes from 0.26.0 to 0.27.0 while its rand edge switches from 0.9.5 to 0.8.8.
The full lockfiles used for verification are from base
`6235c0ad53f66273bee3a357d5687e97afeca635` and head
`42716b7da1f17ca686c04ac40b5ee4a40427396f` in `coreos/coreos-installer`.

## License

Public domain, under [the Unlicense](LICENSE).
