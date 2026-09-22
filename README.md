# Firefox split-view kiosk on a NUC

Displays two dashboard URLs side by side using Firefox's native split view on an
x86_64 Intel NUC. The host runs openSUSE MicroOS and seatd. A Podman Quadlet
runs Cage and Firefox in a container.

Restarting the kiosk service discards browser state. Dashboards must work without
persistent cookies, logins, or local storage.

## Building and testing locally

Install Podman on Linux, and run the following commands from the repository
root:

```bash
podman build --pull=always --no-cache \
  -f container/Containerfile -t localhost/firefox-kiosk:test .
tests/container-smoke.sh localhost/firefox-kiosk:test
```

The smoke test starts one fresh container, waits for both browser page-load
callbacks, checks the running processes after a short delay, and verifies shutdown.
It also rejects logged chroot failures, subprocess exits on SIGABRT or SIGSEGV,
and abnormal VideoBridge closures before shutdown.

All kiosk launch configurations drop other capabilities and retain `SYS_CHROOT`.
The default container seccomp profile needs this capability to allow Firefox's
sandbox helper to call `chroot()`.

Preview the split view from a Wayland desktop:

```sh
tests/run-wayland.sh localhost/firefox-kiosk:test \
  'https://dashboard.example.org/one' \
  'https://dashboard.example.org/two'
```

The preview uses host networking, so loopback URLs reach servers running on your
desktop.

Use a trusted image: the preview shares your Wayland socket and host network,
and disables SELinux labeling for the container. Check that both dashboards
appear together without extra tabs or prompts. Headless tests do not verify the
visible layout or NUC hardware.

## Installing on the NUC

Requirements:

- A NUC with a display, wired DHCP, access to openSUSE repositories and the container registry.
- A fresh host with user and group IDs 1000 available.
- A preparation machine with Python 3, OpenSSL, Buildah, and Podman.
- A container image that supports anonymous pulls.

### Generating the configuration

First, generate the combustion setup via the python script:

```sh
python3 -m kiosk.configure \
  --left 'https://dashboard.example.org/one' \
  --right 'https://dashboard.example.org/two' \
  --ssh-key "$HOME/.ssh/id_ed25519.pub" \
  --ssh-key "$HOME/.ssh/id_rsa.pub"
```

Enter and confirm a root recovery password in the terminal prompts.
SSH access requires the supplied key. Password based SSH login is disabled.

Files are written to `build/nuc-config/combustion/`. Existing output directories
are refused and not overwritten. Use `--output PATH` for another
configuration. If desired, add `--extra-package PACKAGE` to install additional
host packages.

Keep the generated configuration, ISO, and USB drive private: they contain the
root password hash.

### Building the installation ISO

Download the official x86_64 **Container Host SelfInstall** ISO from the
[MicroOS download
page](https://download.opensuse.org/tumbleweed/appliances/iso/openSUSE-MicroOS.x86_64-ContainerHost-SelfInstall.iso). Verify
its published checksum/signature, then build the image:

```sh
./build-installer.sh \
  openSUSE-MicroOS.x86_64-ContainerHost-SelfInstall.iso \
  build/nuc-kiosk.install.iso
```

Pass a custom configuration root as the third argument if you used `--output`
with `python3 -m kiosk.configure`.
Test the finished ISO in a disposable virtual machine with a virtual disk.
Check disk identification, erase confirmation, and cancellation before
installing on hardware.

### Writing the USB and installing

Identify the USB device, then unmount its partitions:

```sh
lsblk -o NAME,MODEL,SERIAL,SIZE,TRAN,MOUNTPOINTS
```

Replace `/dev/sdX` with the USB's device path. **This erases that device.**

```sh
sudo dd if=build/nuc-kiosk.install.iso of=/dev/sdX bs=4M status=progress conv=fsync
sync
```

Boot the NUC from USB and follow the disk-selection and erase-confirmation prompts.
Keep the USB connected through the installed system's first boot so Combustion can
provision the host. Select the SSD for that boot if necessary. Remove the USB after
provisioning. Cancel if the installer starts again!


## Managing the kiosk

Find the NUC's address in your DHCP leases or your router's web interface or
find it via NMAP and connect:

```sh
ssh root@NUC_IP
```

Run these commands as root on the NUC:

```sh
systemctl status seatd.service kiosk.service
journalctl -b -u combustion.service -u seatd.service -u kiosk.service
```

Change dashboards in `/etc/kiosk/kiosk.env`, using plain values without shell
quoting:

```text
KIOSK_URL_LEFT=https://dashboard.example.org/one
KIOSK_URL_RIGHT=https://dashboard.example.org/two
```

Apply URL changes with `systemctl restart kiosk.service`.

To update or roll back, set `Image=` in `/etc/containers/systemd/kiosk.container`
to a tested digest, then run:

```sh
systemctl daemon-reload
systemctl restart kiosk.service
```

Image deployment is manual. Keep a known-working digest for rollback.

## Running source checks

The source tests cover configuration generation, selected input checks, Firefox
session encoding, startup arguments, and browser callback detection.
They use temporary files and local sockets. Startup tests replace the graphics
launch with a stub. They do not build containers or run installation or provisioning.

Install Python 3, OpenSSL, system liblz4, and ShellCheck:

```bash
python3 -m tests.selftest
shellcheck build-installer.sh container/kiosk-start host/combustion/script tests/*.sh
```

To include Quadlet validation, install the target Podman version. The test uses
the systemd generator at
`/usr/lib/systemd/system-generators/podman-system-generator` and skips when it
is absent. That test can also be skipped by setting `KIOS_SKIP_QUADLET_TEST=1`:

```bash
python3 -m tests.selftest
```
