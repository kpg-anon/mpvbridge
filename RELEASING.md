# Releasing

## Versioning

`MAJOR.MINOR.PATCH`. **0.x means beta**: the wire protocol and the settings layout may still change
between minor versions.

The app and the daemon ship together and are expected to carry the same version. Each half keeps it
in exactly one place:

| half | file | key |
| --- | --- | --- |
| app | `android-app/gradle.properties` | `mpvbridge.version` |
| daemon | `daemon/src/mpvbridge/__init__.py` | `__version__` |

Everything else derives from those. `versionCode` is computed in `build.gradle.kts` as
`major * 10000 + minor * 100 + patch`, so it always increases and nobody has to remember to bump it.
The daemon's `pyproject.toml` reads `__version__` through hatchling rather than repeating it.

`.github/workflows/release.yml` refuses to publish unless both files equal the tag. That check
exists because a mismatch would ship an APK whose in-app *About* screen disagrees with the release
it came from.

When to bump what:

- **PATCH** — a fix that changes no behaviour anyone configured. Never changes the protocol.
- **MINOR** — new features, a protocol change, a settings key moving or changing meaning.
- **MAJOR** — reserved for 1.0, which means the protocol is settled.

## Cutting a release

1. Update both version files to the new version.
2. Move the `## [Unreleased]` entries in `CHANGELOG.md` under a new `## [X.Y.Z] - YYYY-MM-DD`
   heading, and update the link definitions at the bottom.
3. Commit, then tag and push:

   ```console
   git commit -am "Release vX.Y.Z"
   git tag -a vX.Y.Z -m "vX.Y.Z"
   git push origin main vX.Y.Z
   ```

The tag push triggers `release.yml`, which builds the signed APK and the Python sdist, verifies the
versions, and creates the GitHub Release with both attached and the changelog section as the body.

## Signing

Release APKs are signed with a keystore that is **never** in the repo. The build reads it from the
environment, and simply produces an unsigned APK when those variables are absent — which is what a
fork or a CI run without secrets gets.

| variable | secret |
| --- | --- |
| `MPVBRIDGE_KEYSTORE` | path to the `.jks`, written by CI from `KEYSTORE_BASE64` |
| `MPVBRIDGE_KEYSTORE_PASSWORD` | `KEYSTORE_PASSWORD` |
| `MPVBRIDGE_KEY_ALIAS` | `KEY_ALIAS` |
| `MPVBRIDGE_KEY_PASSWORD` | `KEY_PASSWORD` |

### Creating the keystore, once

```console
keytool -genkeypair -v \
  -keystore mpvbridge-release.jks \
  -alias mpvbridge \
  -keyalg RSA -keysize 4096 -validity 10000 \
  -dname "CN=mpvbridge, O=kpg-anon, C=US"
```

Keep it somewhere outside the repo and back it up. **If it is lost, no future release can upgrade
an installed copy in place** — Android refuses an update signed by a different key, so every user
would have to uninstall and lose their favorites.

Then add four repository secrets under Settings → Secrets and variables → Actions:

```console
base64 -w0 mpvbridge-release.jks    # -> KEYSTORE_BASE64
```

plus `KEYSTORE_PASSWORD`, `KEY_ALIAS` (`mpvbridge`) and `KEY_PASSWORD`.

### Building a signed APK locally

```console
export MPVBRIDGE_KEYSTORE=/path/to/mpvbridge-release.jks
export MPVBRIDGE_KEYSTORE_PASSWORD=...
export MPVBRIDGE_KEY_ALIAS=mpvbridge
export MPVBRIDGE_KEY_PASSWORD=...
cd android-app && ./gradlew :app:assembleRelease
```

Verify what came out:

```console
apksigner verify --print-certs app/build/outputs/apk/release/app-release.apk
```
