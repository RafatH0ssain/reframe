# reFrame – the ePaper camera

<img src="docs/images/reframe-hero.jpg" alt="reFrame ePaper camera in hand" width="720" />

reFrame is an experimental digital camera designed to capture and display one photo at a time.

reFrame uses a 6-color ePaper display, giving each photo a distinctive dithered texture. After you press the shutter, the display takes about 20 seconds to refresh — colors cycle through as electrical charges physically move ink particles into place, like a digital polaroid.

The photo stays on screen even after powering off. The only way to clear it is to take a new one. When not in use, reFrame doubles as a desk photo frame.

🌐 **[reframe.camera](https://reframe.camera)**

<p align="left">
  <img src="docs/images/sample-dithered-2.png" alt="Dithered photo sample" width="230" />
  <img src="docs/images/sample-dithered-1.png" alt="Dithered photo sample" width="230" />
  <img src="docs/images/sample-dithered-3.png" alt="Dithered photo sample" width="230" />
</p>

## What's Inside

The open source version of reFrame uses only off-the-shelf components so anyone can build their own.

| Component | Part |
|---|---|
| Computer | [Raspberry Pi Zero 2 W](https://www.pishop.us/product/raspberry-pi-zero-2w-with-headers/) |
| Camera | [Raspberry Pi Camera Module 3](https://www.digikey.com/en/products/detail/raspberry-pi/SC1223/17278639) |
| Display | [Waveshare 4" ePaper Spectra 6](https://www.waveshare.com/4inch-e-paper-hat-plus-e.htm) |
| Battery | [PiSugar 3](https://www.pisugar.com/products/pisugar-3-raspberry-pi-zero-battery) |
| Enclosure | 3D printed PLA |

See the full [bill of materials](docs/build-guide.md#materials).

<img src="docs/images/reframe-in-hand.jpg" alt="reFrame ePaper camera in hand" width="460" />

## Getting Started

**→ [Build Guide](docs/build-guide.md)** — How to assemble the camera hardware

**→ [Software Setup](docs/software-setup.md)** — How to install and configure the software on your Pi

**→ [Hardware Porting Notes](docs/hardware-porting.md)** — How to adapt the code for other e-ink displays, cameras, or buttons

**→ [Dashboard Extensions](docs/dashboard-extensions.md)** — Upload your photos directly from the camera to Are.na + more.

Once you have the hardware assembled and Raspberry Pi OS flashed, run:

```bash
sudo apt update
sudo apt install -y git
git clone https://github.com/kaloyaan/reframe.git
cd reframe
chmod +x install.sh
./install.sh
```

## Using the Camera

  <img src="docs/images/reframe.gif" alt="reFrame ePaper camera in action" width="720" />

reFrame is extremely minimalist by design. It has only a single button; no viewfinder, flash or zoom.

1. Press the combined power + shutter button to wake the camera and take a photo.
2. The original image is saved, then dithered to a 6-color palette to match the screen. The processed image is sent to the ePaper display.
3. The ePaper screen takes about 20 seconds to display the photo.
4. Press the button again to take a new photo. It'll replace the previous one on the display.
5. When you turn the camera off, the last photo will stay on the screen.

A web dashboard, accessible over the local network, lets you browse photos, download originals, change the displayed image, adjust camera settings, and optionally upload dithered photos to Are.na.

<img src="docs/images/reframe-dashboard.webp" alt="reFrame dashboard showing the photo gallery and camera controls" width="960" />

## Repository Structure

```
reframe/
├── reframe.py                 # main camera application
├── dashboard.py               # web dashboard for photo management
├── dashboard_proxy.py         # local port 80 dashboard proxy
├── install.sh                 # one-command setup script
├── settings.example.json      # default configuration
├── scripts/                   # hardware and privileged system helpers
│   ├── enable_hdr.sh
│   ├── reframe-apply-update
│   └── reframe-rtc-sync
├── systemd/                   # camera, dashboard, and RTC service units
│   ├── reframe.service
│   ├── reframe-dashboard.service
│   ├── reframe-dashboard-proxy.service
│   ├── reframe-rtc-restore.service
│   └── reframe-rtc-update.service
├── templates/                 # dashboard HTML template
├── static/                    # dashboard CSS and JS, served at /static
├── waveshare_epd/             # e-ink display drivers
├── hardware/                  # 3D-printable enclosure files
└── docs/                      # build guide & software setup
    ├── build-guide.md
    ├── software-setup.md
    ├── hardware-porting.md
    ├── dashboard-extensions.md
    └── images/
```


## License

Software in this repository is licensed under the [Apache License 2.0](LICENSE), unless a file says otherwise.

Hardware design files in [hardware/](hardware/) are licensed under [CC BY 4.0](hardware/LICENSE.md).

The build guide and documentation are provided for reuse with attribution where original to this project. Photos may include third-party products, packaging, logos, or trademarks, which are not licensed by this project.

The reFrame name, logo, domain, and branding are reserved.

## Credits

reFrame is made by [Kaloyan Kolev](https://kaloyankolev.com) with help from lots of friends and family. It started as a thesis prototype at Yale under the name eink.cam.

Thank you to [APOSSIBLE](https://apossible.com) for their generous support of this project.

reFrame logo + branding by [Kevin Chen](https://kevinnchen.com/).

More updates on [Instagram](https://instagram.com/reframe.camera).
