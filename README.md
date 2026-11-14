# FastShare

Quick Share-style file sharing between Windows PCs on the same Wi-Fi. Any size, at full network speed, no internet needed.

## Use it
1. Open FastShare on both PCs. They show up under **Nearby devices** within a few seconds.
2. Drop files or folders on the window (or use **Select files** / **Select folder**).
3. Click a device to send. You can also drop files straight onto a device card.
4. The other PC gets a pop-up to accept. Files go to `Downloads\FastShare`.

If a file was already received before, the other PC is told it's **already downloaded** and can skip it or download it again.

## First run
- Windows Firewall asks for access: tick **Private networks** and allow.
- Your Wi-Fi must be set to **Private** (Settings > Network & internet > Wi-Fi > your network).
- "Windows protected your PC": click **More info > Run anyway** (the app isn't code-signed).

## Get the exe
Every push builds `FastShare.exe` on GitHub (Actions tab > latest run > **FastShare-exe**). The build also runs all tests and a self-test of the real window. Or run `Build_EXE.bat` locally.

Ports: 52525 TCP (transfers), 52526 UDP (finding devices).
