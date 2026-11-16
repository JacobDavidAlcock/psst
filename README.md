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
- **Preview:** click a file to see it. Images, video, audio and PDFs open in the page. In image previews, use the arrow keys or swipe to move between photos.
- **Read text and code:** code opens with line numbers and colours. Markdown opens formatted, CSV files open as a table, and minified JSON opens indented. Use the buttons at the top to switch back to the raw text.
- **Download:** click the download arrow next to a file. To get a whole folder as one file, click **Zip**.
- **Share text:** open **Clipboard**, paste some text and click **Share**. Every device with the page open sees it within 2 seconds and can copy it.
- **Open on your phone:** click **Connect** to show a QR code for the folder you're looking at.

## Manage files

To select a file, click its checkbox. To select a run of files, click the first checkbox, then Shift-click the last. A bar appears at the bottom of the page with these actions:

| Action | What it does |
|---|---|
| **Download** | Downloads one file as it is, or several files as one `.zip`. |
| **Move** | Opens a folder picker. Pick a folder and click **Move here**. |
| **Rename** | Renames the file. Only shows when one file is selected. |
| **Delete** | Deletes the files. Click **Undo** in the message that appears to bring them back. |

Keyboard shortcuts: Ctrl+A selects everything, Delete deletes the selection, F2 renames, and Esc clears the selection.

Deleted files go to a hidden `.psst-trash` folder inside the shared folder. psst empties anything older than 7 days when it starts or when you delete something.

## Resume a stopped upload

If your connection drops during an upload, psst keeps trying for a few minutes and carries on from where it stopped. You don't need to do anything.

If you close the page or the upload gives up, pick the same file again and upload it to the same folder. psst finds the part it already has and sends only the rest.

psst deletes parts of uploads that haven't been touched for 2 days.

## Browse big photo folders

In grid view, psst shows small previews instead of full photos, so a folder of phone pictures opens quickly. Each preview is about 10 KB instead of several MB.

Without anything extra installed, previews work for JPEG photos from phones and cameras, because those photos carry a small preview inside them. If you want sharper previews for every image type, install Pillow on the computer running psst:

```sh
pip install pillow
```

When psst finds Pillow, the startup message says `Pillow thumbnails`.

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
| `-r`, `--read-only` | Let people browse and download, but not upload, create folders, move, rename or delete. |
| `--password PASSWORD` | Ask for this password before showing anything. Any username works. You can set `PSST_PASSWORD` instead. |
| `--no-qr` | Don't print the QR code. |

For example, to hand out a folder without anyone adding to it:

```sh
python psst.py ~/Music --read-only
```

## Before you use it

- psst is for networks you trust, like your home Wi-Fi. It uses plain HTTP, so `--password` keeps out casual visitors but doesn't encrypt anything.
- The shared clipboard lives in memory. It empties when psst stops.
- Anyone who can open the page can move, rename and delete files. If you don't want that, start psst with `--read-only`.
