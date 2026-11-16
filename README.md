<p align="center">
  <img src="assets/psst.png" alt="Two grumpy gnomes sitting cross-legged, one with a laptop and one with a phone, passing a picture file along a tin-can telephone" width="560">
</p>

<h1 align="center">psst</h1>

<p align="center"><em>psst... want some files?</em></p>

psst moves files, photos and text between your computers and phone over your home network. It works like `python -m http.server`, with a web page you can upload to, previews of what you're sharing, and a clipboard every device can see.

It's one Python file. It needs Python 3.8 or later and nothing else.

## Share a folder

1. Open a terminal in the folder you want to share, and run psst:

   ```sh
   python psst.py
   ```

   psst prints the addresses it's listening on:

   ```text
     psst 0.1.0  serving C:\Users\you\Downloads
     uploads enabled

     Local:    http://localhost:8000/
     Network:  http://192.168.1.196:8000/

     Ctrl+C to stop
   ```

   Under the addresses there's a QR code that your phone can scan.

2. On your other device, open the `Network` address in a browser, or scan the QR code.

3. Press Ctrl+C in the terminal to stop sharing.

If Windows asks whether Python can use the network, allow it on private networks. If you don't, other devices can't connect.

If port 8000 is taken, psst uses the next free port, up to 8019. Use the address it prints.

## Install the `psst` command

If you want to run `psst` from any folder, install it from this folder:

```sh
pip install .
```

Now you can share any folder by name:

```sh
psst ~/Downloads
```

## What you can do in the browser

- **Upload:** drag files or whole folders onto the page, or click **Upload**. To upload a screenshot, press Ctrl+V. If a file with the same name already exists, psst keeps both: a second `photo.jpg` arrives as `photo (1).jpg`.
- **Preview:** click a file to see it. Images, text and code, video, audio and PDFs open in the page. In image previews, use the arrow keys or swipe to move between photos.
- **Download:** click the download arrow next to a file. To get a whole folder as one file, click **Zip**.
- **Share text:** open **Clipboard**, paste some text and click **Share**. Every device with the page open sees it within 2 seconds and can copy it.
- **Open on your phone:** click **Connect** to show a QR code for the folder you're looking at.

## Use psst from a terminal

All of these use curl, which comes with Windows, macOS and most Linux systems. Replace `localhost:8000` with the address psst printed.

List a folder:

```console
$ curl http://localhost:8000/
photos/
notes.txt
```

Upload a file into the shared folder:

```console
$ curl -T notes.txt http://localhost:8000/
{"name": "notes.txt", "path": "notes.txt", "size": 18}
```

Download a file:

```sh
curl -O http://localhost:8000/notes.txt
```

If a large download stops partway, add `-C -` to the same command to resume it:

```sh
curl -C - -O http://localhost:8000/big-video.mp4
```

Download a folder as a zip:

```sh
curl -o photos.zip "http://localhost:8000/photos/?zip"
```

## Options

| Option | What it does |
|---|---|
| `directory` | Folder to share. Default: the current folder. |
| `-p PORT`, `--port PORT` | Use this port only. Default: 8000, or the next free port up to 8019. |
| `-b ADDRESS`, `--bind ADDRESS` | Listen on one network address only. Default: `0.0.0.0`, which means every address. |
| `-r`, `--read-only` | Let people browse and download, but not upload or create folders. |
| `--password PASSWORD` | Ask for this password before showing anything. Any username works. You can set `PSST_PASSWORD` instead. |
| `--no-qr` | Don't print the QR code. |

For example, to hand out a folder without anyone adding to it:

```sh
python psst.py ~/Music --read-only
```

## Before you use it

- psst is for networks you trust, like your home Wi-Fi. It uses plain HTTP, so `--password` keeps out casual visitors but doesn't encrypt anything.
- The shared clipboard lives in memory. It empties when psst stops.
- You can't delete or rename files from the web page yet.
