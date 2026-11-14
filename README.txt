FASTSHARE  -  fast file sharing over the same Wi-Fi
====================================================

GET THE APP (pick one)
  A) Double-click Build_EXE.bat  -> makes FastShare.exe in this folder (needs Python from python.org, one time).
  B) Upload this folder to a GitHub repo -> Actions tab -> "Build FastShare.exe" -> download the artifact.
  C) Just test it: double-click Run_without_building.bat (needs Python).

FIRST RUN (important)
  * Windows Firewall will pop up -> tick "Private networks" -> Allow.
  * Your Wi-Fi must be set to "Private" (Settings > Network & internet > Wi-Fi > your network > Private).
    On "Public" Windows blocks other devices from reaching you.

PC TO PC
  1. Open FastShare on both PCs.
  2. The other PC appears in "Choose a device" in a few seconds (or type its IP).
  3. + Files / + Folder  ->  Send.  The other PC clicks Yes.
  Received files go to Downloads\FastShare (changeable). Folders keep their structure.

PHONE
  Tick "Phone / browser sharing" and open the address shown (e.g. http://192.168.1.20:8080/a1b2c3)
  in your phone's browser. Upload files to the PC, or download whatever is in the PC's file list.
  The random code in the address stops other people on the Wi-Fi from getting in.

SPEED TIPS
  * Speed is limited by Wi-Fi, not the app. 5 GHz / Wi-Fi 6 band >> 2.4 GHz.
  * Fastest: both devices near the router, or one PC on an Ethernet cable.
  * Typical: Wi-Fi 5 = 30-60 MB/s, Wi-Fi 6 = 60-120 MB/s, Ethernet both = ~110 MB/s.

Ports used: 52525 (transfers), 52526 (finding devices), 8080 (phone page).
