# Android release signing

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

After the workflow changes are committed and pushed, open **Actions → Android
Player Build → Run workflow** on that branch. CLI equivalent:

```sh
gh workflow run android-build.yml --repo ViciousSquid/Fio --ref CANARY
gh run list --repo ViciousSquid/Fio --workflow android-build.yml
gh run watch RUN_ID --repo ViciousSquid/Fio --exit-status
gh run download RUN_ID --repo ViciousSquid/Fio --name fio-player-release-signed-apk --dir release-apk
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

Install Android SDK Build Tools 35 or newer and put their directory on `PATH`.
These commands prompt for passwords; they do not include passwords in the shell:

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
