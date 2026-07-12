# Publishing the Homebrew tap

This repo ships a **scaffold** formula at [`Formula/internet-historian.rb`](../Formula/internet-historian.rb)
so people on macOS can `brew install` Internet Historian instead of using `pipx`. It targets a
**personal tap** — your own `homebrew-tap` repo — not homebrew-core. (homebrew-core has its own
review process and stricter rules; don't submit there from this scaffold.)

A tap is just a GitHub repo named `homebrew-<something>`. Once published, users run:

```bash
brew tap ssskay/tap
brew install internet-historian
```

## One-time: create the tap repo

```bash
# Create an empty public repo named exactly `homebrew-tap` under your account.
gh repo create ssskay/homebrew-tap --public --description "Homebrew tap for Internet Historian"

git clone https://github.com/ssskay/homebrew-tap
cd homebrew-tap
mkdir -p Formula
```

`brew tap ssskay/tap` maps to the repo `ssskay/homebrew-tap` — Homebrew inserts the `homebrew-`
prefix for you.

## Each release: copy the formula in

1. **Cut the release** in this repo (already done for v0.2.0):

   ```bash
   git tag v0.2.0
   ./scripts/build_skill.sh            # produces internet-historian.skill (reproducible)
   gh release create v0.2.0 --generate-notes \
       dist/internet_historian-0.2.0.tar.gz \
       dist/internet_historian-0.2.0-py3-none-any.whl \
       internet-historian.skill
   ```

   Attaching the sdist (`internet_historian-0.2.0.tar.gz`) is what the formula's `url` points at.
   Attaching `internet-historian.skill` lets people install the Claude skill straight from
   Releases (see the README's "Install as a Claude skill"). If the release already exists,
   attach the skill on its own with
   `./scripts/build_skill.sh && gh release upload v0.2.0 internet-historian.skill`.

2. **Confirm the `url` + `sha256`** in `Formula/internet-historian.rb` match that sdist:

   ```bash
   shasum -a 256 dist/internet_historian-0.2.0.tar.gz
   ```

   The value already committed here was generated from the v0.2.0 sdist. If you re-cut the
   release and the tarball bytes change, update `sha256` to the new digest.

3. **Refresh the dependency resources** when `requests` (or its deps) move, so the pins stay
   current:

   ```bash
   brew update-python-resources internet-historian
   ```

   That rewrites the `resource "requests"` / `certifi` / `charset-normalizer` / `idna` /
   `urllib3` blocks with fresh URLs and hashes.

4. **Copy the formula into the tap and push:**

   ```bash
   cp Formula/internet-historian.rb ../homebrew-tap/Formula/
   cd ../homebrew-tap
   git add Formula/internet-historian.rb
   git commit -m "internet-historian 0.2.0"
   git push
   ```

## Test it locally before you push

```bash
# Lint the formula.
brew style Formula/internet-historian.rb
brew audit --strict --online Formula/internet-historian.rb

# Actually build + install it from source and run the `test do` block.
brew install --build-from-source --verbose Formula/internet-historian.rb
brew test internet-historian
```

### Build-backend note

Internet Historian builds with **Hatchling** (see `pyproject.toml`). If `brew install
--build-from-source` fails while building the sdist because the sandbox can't fetch the PEP 517
backend, add the build backend as resources (`hatchling` and its deps) or install from the
prebuilt wheel. For a pure-Python, single-module package like this one, the simplest fallback is
to point the formula's `url` at the published **wheel** and skip the source build. The runtime
footprint is tiny: one module (`historian.py`) plus `requests`.

## Updating for future versions

Bump `__version__` in `historian.py`, rebuild (`python -m build`), cut a new GitHub release with
the new sdist **and** the freshly built `internet-historian.skill` attached (run
`./scripts/build_skill.sh` — see step 1), then repeat steps 2–4 above with the new version
number, tarball hash, and refreshed resources.
