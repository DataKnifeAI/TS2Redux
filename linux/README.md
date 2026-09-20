# TS2 Redux Linux Edition

Proton installer for the Homefront: The Revolution TimeSplitters 2 port.
You must own Homefront on Steam (`223100`). This repo does not ship the game.

```bash
# after cloning this branch
./bin/ts2redux install --shutdown-steam

# or
curl -fsSL https://raw.githubusercontent.com/DataKnifeAI/TS2Redux/linux-edition/install.sh | bash
```

`ts2redux doctor` checks Steam, Homefront MD5s, Redux DLLs, the lowercase launcher, and launch options.

`WINEDLLOVERRIDES=dinput8=n,b %command%` is written onto Homefront and onto a **TimeSplitters 2** library shortcut that boots `Bin64/timesplitters2.exe`.

See the [project README](../README.md) for what Redux unlocks. The Windows `.exe` installer is not used.
