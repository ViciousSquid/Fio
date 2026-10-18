# Android release signing

Use GitHub Actions for a repeatable release build, or sign an existing unsigned
APK locally. You need a JDK, one permanent release keystore, its alias/passwords,
and access to the repository's Actions secrets if using CI. You do not need to
build Fio on your own computer to use the CI route.

The committed `fio-player-arm64-debug.apk` is installable for testing, but is
signed with a debug identity. The cloud-built release is **unsigned** until you
sign it. No production release key was created during this work.

The **Android Player Build** workflow produces a signed debug APK and a release
APK. Debug builds need no repository secrets. A release APK is installable only
after signing. The workflow signs it when all four secrets below are available;
otherwise the artifact is explicitly named `fio-player-release-unsigned-apk`.
Partial credentials, bad passwords, an invalid alias, or a failed signature check
fail the job. Pull request builds always produce an unsigned release APK.

## Check the repository configuration

Open [Fio repository Actions secrets](https://github.com/ViciousSquid/Fio/settings/secrets/actions)
as a repository administrator. Under **Repository secrets**, check these names:

| Repository secret | Value |
| --- | --- |
| `FIO_ANDROID_KEYSTORE_BASE64` | Single-line base64 of the complete release `.jks` or `.keystore` file |
| `FIO_ANDROID_KEY_ALIAS` | Private-key alias inside that file |
| `FIO_ANDROID_KEYSTORE_PASSWORD` | Keystore password |
| `FIO_ANDROID_KEY_PASSWORD` | Private-key password; for PKCS12 this normally equals the keystore password |

You can also list names without revealing values:

```sh
gh auth login
gh secret list --repo ViciousSquid/Fio
```

GitHub never returns saved secret values. Four matching names establish only
that they exist: the signing step must succeed to prove that the file, alias and
passwords match. Organization secrets must allow this repository. Environment
secrets are not used by this workflow because its APK job has no `environment`.

During cloud setup, both the GitHub CLI and a direct API request were denied
with HTTP 403 by the environment's network proxy. Existing repository secret
names and values could not be verified here; this does **not** establish that
they are missing.

## Where to find an existing signing key

There is no standard Android release-key location. Look on the computer that
built the old release, in its secure backups/password manager, and in the
original CI configuration. Ask whoever produced the last distributed APK.
Common filename extensions are `.jks`, `.keystore`, `.p12` and `.pfx`.

To locate candidate files on your own machine (these commands print paths,
not key contents):

```sh
# Linux or macOS
find "$HOME" -type f \( -iname '*.jks' -o -iname '*.keystore' -o -iname '*.p12' -o -iname '*.pfx' \) 2>/dev/null
```

```powershell
# Windows PowerShell
Get-ChildItem $env:USERPROFILE -Recurse -Force -File -Include *.jks,*.keystore,*.p12,*.pfx -ErrorAction SilentlyContinue | Select-Object -ExpandProperty FullName
```

`~/.android/debug.keystore` (Windows: `%USERPROFILE%\.android\debug.keystore`)
is the normal **debug** key, not a production signing identity. Do not mistake
it for an earlier release key.
The nearby `adbkey` file authorizes ADB connections; it does not sign APKs.

GitHub does not let you download the original values of saved Actions secrets.
An APK contains its public signing certificate, **not the private key**: you
cannot recover a signing key or its password from the APK. To inspect an old
APK's public certificate after installing Android SDK Build Tools:

```sh
apksigner verify --verbose --print-certs old-fio-player.apk
```

Compare that certificate's SHA-256 fingerprint with the candidate keystore's
certificate using `keytool -list -v`. The matching alias must be a
`PrivateKeyEntry`, and you need its passwords. A matching certificate-only
entry cannot sign releases.

If the app uses **Google Play App Signing**, check the app's **App integrity /
App signing** page in Play Console. Its app-signing certificate and upload
certificate may differ. Google generally holds the app-signing private key;
your local upload key has a different role. A lost upload key can be reset
through Play Console's process, but that does not recover a lost key used to
sign directly distributed APKs. See the
[Android signing documentation](https://developer.android.com/studio/publish/app-signing).

## Choose the signing identity

If an older Fio Player release was distributed, find its original release key
and reuse it. Android updates require the same application ID and signing
certificate. This build uses `org.vicioussquid.fio.fioplayer`; check an old APK's identity
before treating this as an in-place upgrade. A new key cannot update an old APK
signed by a different key. Export worlds before uninstalling an older app:
uninstalling removes its private files. Play App Signing uses separate upload
and app-signing keys; a Play upload key cannot sign a sideloaded APK that updates
the Play-installed app. APKs here are intended for direct installation.

Inspect an existing key with JDK 17 or newer; `keytool` prompts for the password:

```sh
keytool -list -v -keystore /secure/path/fio-player-release.jks
```

The entry must say `PrivateKeyEntry`. Record its alias and certificate SHA-256
fingerprint. Keep the original file and its passwords in separate secure backups.
Do not regenerate a key for each release, commit it, or send its contents in chat.

If you have no prior release identity to preserve, create a new key on your own
trusted machine. Use an encrypted or otherwise private directory **outside the
repository**. The following example uses an interactive password prompt and
creates a JKS key valid for about 27 years:

```sh
keytool -genkeypair -v -storetype JKS \
  -keystore /secure/path/fio-player-release.jks \
  -alias fio-player -keyalg RSA -keysize 3072 -validity 10000
```

Enter the requested identity details, choose a strong keystore password, and
choose a private-key password (press Enter to use the same password). Save both
in a password manager and back up the keystore before proceeding. Losing this
key prevents ordinary updates to directly installed releases signed with it.

On Windows, an example location is
`C:/Users/YOUR_NAME/secure-fio-keys/fio-player-release.jks`. Create the directory
first. Use your real path in every command below. Enter multiline shell examples
as one line in PowerShell, or use PowerShell's own continuation syntax.
The JDK supplies `keytool`; Android Studio's bundled JDK can also provide it.

## Save the four secrets

The simplest route is **Settings → Secrets and variables → Actions → New
repository secret** at the link above. Enter each value under its exact name.
Generate the base64 string locally, without uploading the keystore to a web
encoder. Base64 is an encoding of the private key file, not encryption.

With GitHub CLI and Python installed on Linux, macOS or Windows, this command
pipes the encoded key directly into GitHub without displaying it or creating a
second secret file. Substitute your local keystore path:

```sh
python -c "import base64,pathlib,sys; sys.stdout.write(base64.b64encode(pathlib.Path('/secure/path/fio-player-release.jks').read_bytes()).decode())" | gh secret set FIO_ANDROID_KEYSTORE_BASE64 --repo ViciousSquid/Fio
gh secret set FIO_ANDROID_KEY_ALIAS --repo ViciousSquid/Fio
gh secret set FIO_ANDROID_KEYSTORE_PASSWORD --repo ViciousSquid/Fio
gh secret set FIO_ANDROID_KEY_PASSWORD --repo ViciousSquid/Fio
```

The last three commands prompt for their values. Enter `fio-player` for the
alias if you used the generation command above. Enter each password exactly as
created; no surrounding quotes or trailing newline. On Windows, a path such as
`C:/Users/you/secure/fio-player-release.jks` works in the Python command. Do not
put passwords in command arguments, shell history or workflow YAML.

If using the GitHub web form for the encoded key, copy a single-line string
locally with one of the following, paste it only into the secret form, then
clear the clipboard:

```sh
# macOS
base64 < /secure/path/fio-player-release.jks | tr -d '\n' | pbcopy
# Linux with Wayland
base64 -w 0 /secure/path/fio-player-release.jks | wl-copy
```

```powershell
# Windows PowerShell
[Convert]::ToBase64String([IO.File]::ReadAllBytes('C:\secure\fio-player-release.jks')) | Set-Clipboard
```

## Build, verify and install

Matching `CANARY` pushes trigger **Android Player Build** automatically. Open
**Actions → Android Player Build** and select the latest `CANARY` run. Once the
four secrets are configured, use **Re-run all jobs** on that run. This rebuilds
and signs using the current secret values without changing source code.

A rerun uses the original run's commit. If a run failed because of a source or
workflow defect, use the new push-triggered run containing the fix; adding
signing secrets cannot repair a failure earlier in compilation.

The repository's current default branch is `3.0.0.1010`; the Android workflow
does not exist there. GitHub requires its workflow file on the default branch
before **Run workflow** / `workflow_dispatch` is available. Use the push run or
its rerun for now. Moving the workflow onto the default branch is a separate
repository change; no default-branch changes were made here.

CLI commands for listing and rerunning an existing push run:

```sh
gh run list --repo ViciousSquid/Fio --branch CANARY --limit 20
gh run rerun RUN_ID --repo ViciousSquid/Fio
gh run watch RUN_ID --repo ViciousSquid/Fio --exit-status
gh run download RUN_ID --repo ViciousSquid/Fio --name fio-player-release-signed-apk --dir release-apk
```

Choose the run named **Android Player Build**, not a desktop build. If no runs
appear, check that Actions is enabled in the repository and that the commit
contains `.github/workflows/android-build.yml`. After the workflow exists on the
default branch, a new manual run can also be requested with:

```sh
gh workflow run android-build.yml --repo ViciousSquid/Fio --ref CANARY
```

A successful signing run publishes **fio-player-release-signed-apk**, containing
`fio-player-release.apk`. The signing log prints the public certificate's
fingerprint; compare it with `keytool -list -v` above. The workflow uses
`zipalign -P 16`, signs with Android SDK `apksigner`, verifies the signature and
alignment, checks packaged native libraries for 16 KiB alignment, and deletes
the temporary key file. Credentials enter only the signing step, after Buildozer
finishes. The unsigned artifact name is never a substitute for a signed release.

With Android SDK Build Tools on your machine, independently verify the download:

```sh
apksigner verify --verbose --print-certs release-apk/fio-player-release.apk
zipalign -c -P 16 4 release-apk/fio-player-release.apk
```

Copy the APK to the phone, open it in Files and allow that app to install from
this source when Android requests it. ADB is optional. A release APK cannot
replace a debug APK signed with the debug key, even when their package IDs match;
back up worlds and uninstall the debug build first. Future releases signed with
your same release key can update this release in place.

## Sign a locally built unsigned APK

You can use an unsigned APK downloaded from the CI artifact; you do not have
to compile it again just to sign it. To compile one locally,
follow [README.md's Linux build instructions](README.md#build-locally-on-linux)
and run `buildozer -v android release` from `player/`. The unsigned output is
under `player/bin/`. Use GitHub Actions if you do not have a Linux build setup.

Install Android SDK Build Tools 35 or newer and put their directory on `PATH`.
Android Studio's **SDK Manager → SDK Tools → Android SDK Build-Tools** installs
`apksigner` and `zipalign`. Usual SDK locations are:

| Host | Usual SDK directory |
| --- | --- |
| Windows | `%LOCALAPPDATA%\Android\Sdk` |
| macOS | `~/Library/Android/sdk` |
| Linux | `~/Android/Sdk` |
| Buildozer | `~/.buildozer/android/platform/android-sdk` |

The executables are inside `build-tools/<installed-version>/`. On Windows the
signer is `apksigner.bat`; `keytool.exe` comes from the JDK, not Platform Tools.
ADB is optional for installation and belongs to the SDK's separate
`platform-tools/` directory.

These commands prompt for passwords; they do not include passwords in the shell:

Use the actual downloaded input filename if it differs from
`fio-player-release-unsigned.apk` (local Buildozer outputs also contain the
version and architecture in their names).

```sh
zipalign -P 16 -f 4 fio-player-release-unsigned.apk fio-player-aligned.apk
apksigner sign --ks /secure/path/fio-player-release.jks \
  --ks-key-alias fio-player --out fio-player-release.apk fio-player-aligned.apk
apksigner verify --verbose --print-certs fio-player-release.apk
zipalign -c -P 16 4 fio-player-release.apk
python -m player.tools.check_apk fio-player-release.apk
```

Never modify the APK after signing. If signing fails in Actions, check all four
secrets, confirm the alias is a `PrivateKeyEntry`, and test the same keystore
locally. Fix the saved credentials and rerun; no new release key is necessary.

## Versions, installation and subsequent updates

Keep `org.vicioussquid.fio.fioplayer` as the application ID if the release is
intended to update an existing Fio Player installation. The old and new
signing certificates must also match. Compare both APKs with
`apksigner verify --print-certs` before distributing an update.

Before a later release, update the visible `version` in `player/buildozer.spec`
and make the Android `versionCode` greater than every previously distributed
release. Buildozer accepts an explicit `android.numeric_version` in `[app]`.
For example, **only if it is higher than your previous public version**, a
future update from the current `3.0.0` / `102430000` build could use:

```ini
version = 3.0.1
android.numeric_version = 102430001
```

Check the actual result; do not assume the visible version string is the
numeric version code:

```sh
aapt2 dump badging fio-player-release.apk
```

The `package:` line prints the application ID, `versionCode` and `versionName`.
The current cloud-built unsigned APK has version `3.0.0` and code `102430000`.
This guide does not change the spec or assign your next public release number.

Install the **signed** file through Android's Files app, or with:

```sh
adb install -r fio-player-release.apk
```

`INSTALL_FAILED_UPDATE_INCOMPATIBLE` usually means the installed app has a
different signing identity. Find the original key for a release update. If
you deliberately switch from the supplied debug build to your own release,
preserve original `.fiopak` files and any needed reports outside the app before
uninstalling the debug build. Uninstallation removes private imported files.
`INSTALL_FAILED_VERSION_DOWNGRADE` means the installed version code is higher;
publish a properly numbered update rather than relying on development-only
downgrade options.

Keep the unsigned input, verified signed APK, certificate fingerprint, source
commit, version name/code and test results with each release. Store the private
keystore and passwords in secure, separate backups outside the repository.
Ordinary updates reuse the original release key; do not generate a new key on
each build.

APKs are suitable for direct Fio downloads. Google Play generally requires an
**AAB**, an upload key and Play App Signing configuration. The current Fio
workflow produces APKs; configuring a Play release is a separate distribution
step. Do not upload an unsigned APK or the committed debug build as a release.

The workflow saves build artifacts; it does not automatically publish a GitHub
Release or upload to Google Play. After device acceptance, download the verified
signed APK and attach it, the intended `.fiopak` and their checksums to your
chosen release/download page. Keep the keystore and passwords private.
