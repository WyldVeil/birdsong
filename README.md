# Birdsong

[![Latest release](https://img.shields.io/github/v/release/WyldVeil/birdsong)](https://github.com/WyldVeil/birdsong/releases/latest)
[![CI](https://github.com/WyldVeil/birdsong/actions/workflows/ci.yml/badge.svg)](https://github.com/WyldVeil/birdsong/actions/workflows/ci.yml)

**A 24/7 bird sound identifier with a live web page.** Plug a microphone into
your computer, point it at a window or the garden, and Birdsong listens around
the clock. It identifies birds with [BirdNET](https://birdnet.cornell.edu/)
(Cornell Lab of Ornithology) and shows them on a page you can open in any
browser: what's singing right now, today's visitors, and every day, week,
month and year since you started.

Runs on **Windows, Linux, macOS (Apple Silicon) and Raspberry Pi**. One file to
double-click; no Python install, no admin rights, no account, no cloud.

![The page on a desktop](docs/screenshot-desktop.png)

<p align="center">
  <img src="docs/screenshot-bird.png" width="62%" alt="A bird's page">
  &nbsp;
  <img src="docs/screenshot-phone.png" width="27%" alt="On a phone">
</p>

## What you get

- **Live view.** The latest bird with a photo, a scrolling spectrogram of what
  the microphone hears right now, and "hearing now" as a bird sings.
- **Logbook.** Day, week, month, year and all-time views with previous/next
  navigation: detections per hour or day, a "who sang when" grid, comparisons
  with the previous period, and first song of the day.
- **Bird cards.** Photo cards for every species, sortable by most or fewest
  detections, name, most recently heard, first heard, confidence or rarity.
- **Every detection** with its time, confidence and spectrogram, plus a year
  calendar.
- **Local list.** Every species expected where you live, greyed out until
  heard, so it slowly fills in.
- **Each bird's page.** Photo (with credit), description, a link to Wikipedia,
  a time-of-day clock, the last 90 days and a monthly pattern.
- **Recordings for you only.** The admin can play, download, star (keep forever)
  or delete each clip. Visitors can't hear anything, because a microphone in a
  house picks up more than birds.
- **Extra false-detection filters on top of BirdNET.** A shifted re-check
  catches hits on background noise, a human-noise guard stops sniffs becoming
  "owls", and a per-species slider lets you trust your garden regulars or
  review anything doubtful (see below).
- **Looks after itself.** Clips are deleted after 30 days unless starred, total
  clip space is capped, and recording pauses if the disk gets low.
- **Optional BirdWeather sharing.** Your detections can appear on
  [BirdWeather](https://app.birdweather.com/)'s public world map of listening
  stations.
- **Light on resources.** It uses a few percent of one CPU core on a desktop PC and ~250 MB of RAM. A Raspberry Pi 4 or 5 copes fine.

## Quick start

### Easiest: download a release

Go to **[Releases](https://github.com/WyldVeil/birdsong/releases/latest)** and
download the zip for your computer. Each one contains everything: its own
Python, the libraries and the BirdNET model. There's nothing to install and
nothing more to download.

| Your computer | Zip | Then |
|---|---|---|
| Windows 10/11 (64-bit) | `birdsong-…-windows-x64.zip` | double-click **`run.bat`** |
| Linux PC (64-bit Intel/AMD) | `birdsong-…-linux-x64.zip` | `./run.sh` |
| Raspberry Pi 4/5 (64-bit OS), other ARM64 Linux | `birdsong-…-linux-arm64.zip` | `./run.sh` |
| Mac with Apple Silicon | `birdsong-…-macos-arm64.zip` | `./run.sh` in Terminal |

Unzip it somewhere permanent (e.g. `Documents\birdsong`) and run it. The
first start asks a few setup questions, then your browser opens at
**http://localhost:8080**.

*Windows: if SmartScreen warns about an unrecognised app, choose **More info →
Run anyway**. Mac: allow microphone access for Terminal when asked.*

### From source (git)

```sh
git clone https://github.com/WyldVeil/birdsong.git
cd birdsong
./run.sh          # Windows: run.bat
```

The first run downloads [uv](https://github.com/astral-sh/uv), which fetches
a private Python and the libraries into `.runtime/` (about 300 MB, once).
BirdNET is downloaded on first start.

### Just want to look first?

```
run.bat demo        (Windows)
./run.sh demo       (Linux/macOS)
```

This fills the page with a few months of made-up detections. No microphone is
needed. The admin login is `admin` / `demo`.

## Setup questions

| Question | Why |
|---|---|
| **Where is the microphone?** Type a town, postcode, or `lat, lon`. | BirdNET uses it to know which birds live near you, which cuts out most false identifications. It's rounded to ~1 km, stored only on your computer, and never shown on the page. |
| **Which microphone?** | Lists your inputs and records 4 seconds to check the level: not silent, not clipping. You can also name the mic to show it at the bottom of the page. |
| **Admin password** | Lets you play, keep and delete recordings, and review rare birds. Username is `admin`. |
| **Who can open the page?** | **Just this computer** (localhost, the default), **devices on my home network** (phones and tablets on your Wi-Fi), or **the internet** through your own domain. You can also hide the page under a random secret path. |
| **Title / subtitle** | What the page calls itself. |
| **Share on BirdWeather?** | Optional. Streams detections to BirdWeather's public map (see below). |

Change any of it later with `run.bat setup` / `./run.sh setup`.

## How identification works

Every 1.5 seconds the last 3 seconds of audio go through BirdNET. Species are
grouped using BirdNET's location model:

| Tier | Which species | Needs | Shown publicly |
|---|---|---|---|
| **Expected** | Occur near you in any week of the year, so out-of-season birds still count | 70% confidence | Yes |
| **Unusual** | Plausible within ~150 km | 85% | Yes, with an *Unusual* badge |
| **Rarity** | Anything else BirdNET knows (~6,000 species) | 95% **and** heard twice in a row | Only after you confirm it as admin |

A bird that keeps singing becomes **one** detection that grows, not dozens.
When it stops, the audio around it is saved as an MP3 clip with a
spectrogram. Non-bird sounds (dogs, engines, voices…) are never logged. If
speech is heard during a bird's clip, its spectrogram is hidden from visitors.

Identification is automatic and can be wrong. Admins can hide false positives
with one click.

## Fewer false detections: what Birdsong adds on top of BirdNET

BirdNET is very good, but anyone who runs it 24/7 sees phantom birds: a
"Whimbrel" from the hiss of an empty room, a "Barn Owl" that is really someone
sniffing next to the microphone. Birdsong layers its own checks on top of
BirdNET so these don't reach the public page or BirdWeather. Nothing is ever
thrown away. Anything held back waits in the admin **review list**, where one
click (✓) approves it if you listen and it's real.

| Layer | What it catches | How it works |
|---|---|---|
| **1. Location tiers** | Birds that don't live anywhere near you | BirdNET's own location model, used to require more confidence from less likely species (see the table above). |
| **2. Rarity review** | One-off exotic matches | Species from outside your region need 95% *and* two windows, then your confirmation. |
| **3. Shifted re-check** | Hits on background noise | See below. |
| **4. Human-noise guard** | Sniffs, breathing and coughs mistaken for owls | See below. |
| **5. Overlapping windows** | Real calls cut in half by a window edge | See below. |
| **6. Your own species rules** | Whatever your site needs | A per-species slider in the admin panel. See below. |

### 3. Shifted re-check (for detections heard in one window)

**The problem.** A single 3-second window sometimes scores 70–90% for a bird
that isn't there. BirdNET has latched onto a chance pattern in the background
noise.

**The idea.** A real call is *in the audio*. Shift the 3-second window a
fraction of a second either way and the call is still inside it, so BirdNET
recognises it again. A noise artefact only exists for one exact framing of the
background, and it vanishes when the window moves.

**How it's implemented.** While a bird is heard in only one window, its
detection is marked *checking* and stays private. When the event ends, the
engine takes the **original uncompressed audio** from its in-memory buffer and
re-runs BirdNET with the window shifted by ±0.25, ±0.5, ±0.75 and ±1 second.
The detection is **verified** if the species still scores at least 0.5 in at
least 2 of those 8 shifted windows. Otherwise it's marked **unverified** and
goes to the review list. Detections heard in two or more windows already count
as confirmed.

**Evidence from a real station.** Re-scoring the saved clips of confident Robin
and Jackdaw detections reproduced 0.94–0.98. A "Spotted Crake" logged at 0.88
and three "Whimbrels" at about 0.72 collapsed to 0.00–0.08 when the window
moved. Their recordings contained no calls at all.

### 4. Human-noise guard (for species people can imitate by accident)

**The problem.** A sniff, breath or cough right next to a sensitive microphone
can sound like a **Barn Owl's** hissing screech to BirdNET. The shifted re-check
can't catch this, because a sniff is a real sound and is still there when the
window moves.

**The idea.** BirdNET also recognises human sounds, through its "Human
non-vocal" class. If a person is making noise in the same clip as an owl-like
detection, the detection can't be trusted.

**How it's implemented.** For a short list of noise-prone species, every
detection is held until the bird stops calling, even if it was heard several
times. The engine then checks the whole clip (3-second windows every 0.5 s).
The detection is marked **human noise nearby** and goes to the review list if
either:

- the "Human non-vocal" score reaches 0.25 anywhere, **or**
- a Human class is BirdNET's **single top guess** in any window. This catches
  quieter sniffs that never reach 0.25.

A looser "human anywhere in BirdNET's top few guesses" rule (the idea behind
BirdNET-Pi's privacy filter) was tested and rejected. An owl's hiss genuinely
sounds part-human to BirdNET, so the human classes ranked 2nd to 5th even for
quiet night-time owls, and the rule would have held back those too. Common garden birds are never on the
list, so they're never held back just because someone is nearby.

**How the list was chosen.** On a real station, human noise turned up in the
clips of **48% of Barn Owl** and **25% of Tawny Owl** detections, against
**0–1%** for every other species, including hundreds of Robins and Jackdaws.
In recorded test sniffs, BirdNET's top bird guesses were Barn Owl, then Common
Scoter. Running BirdNET over the 1,840 non-bird recordings of the
[ESC-50](https://github.com/karoldvl/ESC-50) everyday-sound dataset (breathing,
snoring, coughing, typing, doors, dripping water…) produced almost no bird
detections. So the problem is close-range sounds at your own microphone, not
household noise in general. The default list is the five UK owls (Barn, Tawny,
Little, Long-eared, Short-eared) plus Common Scoter, and you can change it with
`human_guard_species`.

### 5. Overlapping windows

BirdNET hears in 3-second windows. Birdsong starts a new window **every
second**, so consecutive windows overlap by 2 seconds and every call is heard
whole in at least one of them. It also means real birds are usually heard in
two or more windows, which counts as confirmed without needing layer 3. A
global study of BirdNET settings ([Pérez-Granados et al. 2025,
*Ibis*](https://bou.org.uk/blog-perez-granados-optimizing-birdnet-lessons-from-a-global-initiative/))
found that raising overlap from the default 0 to about 2 seconds improved
performance, both for detecting calls and for describing which birds are
present. The cost is about one BirdNET run per second: a few percent of one
CPU core on a desktop.

### 6. Your own species rules

Every station is different. A bird that's constant in one garden is a rarity
in another, and some microphones get fooled by sounds peculiar to their room.
So each species has a **detection level** that you can change with a slider:

| Level | What happens |
|---|---|
| **Trusted** | One window is enough. Appears straight away, no extra checks. For birds that are extremely common at your site and that household sounds don't imitate. |
| **Standard** | The normal checks (layer 3). The default for almost every species. |
| **Guarded** | Standard plus the human-noise guard (layer 4). The default for owls and Common Scoter. |
| **Strict** | Guarded, and anything heard in only one window waits for your review. |
| **Review all** | Every detection of this species waits for your ✓. |

How to use it:

- **Open it from any detection.** In admin mode every detection has a rules
  button (sliders icon), which opens the slider for that species, starting at
  its current level.
- **See every changed species at a glance.** The **Species rules** section of
  the admin panel lists every species not at Standard; click one to change it,
  or pick any other species from the box.
- **Changes take effect at once** for new detections and are saved to
  `data/config.json`.
- **Nothing is trusted out of the box.** That's your call, based on what you
  hear.
- **Owls and other noise-prone species warn you** if you lower them below
  Guarded, since sniffs and breathing can then get through.

## Using it

### Commands

Use `run.bat <command>` on Windows and `./run.sh <command>` elsewhere.

| Command | What it does |
|---|---|
| *(nothing)* | Listen and serve the page (runs setup the first time) |
| `setup` | Change location, microphone, password, access, title |
| `demo` | Preview with made-up data |
| `devices` | List microphones |
| `mic-test` | Record 5 seconds and report the level |
| `password` | Change the admin password |
| `birdweather` / `birdweather --off` | Share detections on BirdWeather, or stop |
| `analyse FILE…` | Identify birds in existing recordings (WAV, FLAC, OGG, MP3) and add them to the log |
| `autostart install` / `remove` / `status` | Start automatically (see below) |
| `selftest` | Run the tests and a real identification check |
| `--help` | Everything else |

Add `--port 9000` to `run` or `demo` to use a different port, and
`--no-browser` to not open a browser.

### Start automatically

```
run.bat autostart install        (Windows: starts hidden when you log in)
./run.sh autostart install       (Linux: systemd user service; macOS: LaunchAgent)
```

On **Linux**, to start at boot even when nobody logs in (a headless Pi or home
server), also run once:

```sh
loginctl enable-linger $USER
sudo usermod -aG audio $USER   # lets it open the mic without a desktop login; then reboot
```

On **macOS** you'll be asked to allow microphone access the first time.

### The microphone

**Recommended: a Clippy EM272 mono microphone** (sold by Micbooster), which is
what the author's station uses. Here's why:

- **Very quiet.** Its Primo EM272 capsule has a self-noise of just **14 dBA**,
  quieter than many microphones costing several times as much. It picks up far
  fainter, more distant birdsong than a typical USB mic, and much more than a
  phone's built-in mic, which is noisy and processed to suit speech.
- **Mono is all you need.** BirdNET analyses a single channel of sound, so a
  stereo mic would just be mixed down. Get the *mono* version.

It's a small 3.5 mm mic that plugs into a computer's pink microphone jack (which
supplies the "plug-in power" it needs) or a USB sound card's mic input.

Any microphone works, though. A cheap USB mic will catch nearby birds.

- **Placement matters as much as the microphone.** An open window or outdoors
  is far better than behind glass. Keep it away from fans, fridges and road
  noise if you can.
- **Sensitive mics on onboard sound cards:** with a mic like the EM272, the
  input slider at 100% can add +60 dB of gain and clip constantly. Somewhere
  around 60–70% is usually right; check with `mic-test`.
- **Level:** run `mic-test`. Aim for the room's background noise around
  −60 to −35 dBFS with no clipping. If it clips, turn the input volume down in
  your sound settings (Windows: *Settings → System → Sound → Input*; Linux: your
  desktop's sound settings or `pavucontrol`). The admin panel warns you if it
  clipped in the last hour.
- **Bats** aren't detected. They call in ultrasound, which needs a special
  microphone and a different model.

### Share on BirdWeather (optional)

[BirdWeather](https://app.birdweather.com/) is a free public map of bird
listening stations around the world, powered by BirdNET. Its data has been
used in published research, for example a 2025 *Science* study of how light
pollution lengthens birds' singing day, built on over 60 million detections.
Birdsong can stream each new detection to it as it happens.

1. **Make a free account** at
   [app.birdweather.com/login](https://app.birdweather.com/login). You don't
   need to buy anything. Their own hardware (the "PUC") is optional; a
   computer running BirdNET counts as a station.
2. **Add a station** at
   [app.birdweather.com/account/stations](https://app.birdweather.com/account/stations).
   - **Location:** the pin is shown publicly. Put it somewhere *general*, such
     as a nearby park or the middle of your town, not your house, or turn on
     the station's location privacy. Birdsong never sends coordinates;
     BirdWeather files every detection at the station's pin.
   - **Name:** this is public too, so pick something that doesn't identify you.
3. **Copy the station token** from the station's page.
4. **Run** `run.bat birdweather` (Windows) or `./run.sh birdweather`. Paste the
   token, which Birdsong checks with BirdWeather and confirms the station's name,
   and choose whether to send audio. Then restart Birdsong. (`setup` asks the
   same questions as its last step.)

**What gets sent:**

- **Each detection, once:** species, time and confidence, sent once the bird
  has stopped singing. Only detections made after you switch sharing on are
  sent; nothing older.
- **Never sent:** detections you've hidden as admin, and rarities you haven't
  confirmed.
- **Audio is off by default.** If you turn it on, each detection includes a
  short FLAC clip, but **clips where speech was heard are never sent**.
- **If BirdWeather or your internet is down,** detections queue and are retried
  later. If the token is rejected, uploads pause and the log says why.
- **Status:** the admin panel shows how many were sent, queued or failed. The
  page footer mentions that detections are shared with BirdWeather, linking only
  to its main map, not your station.
- **Turn it off** with `run.bat birdweather --off` / `./run.sh birdweather --off`.

Note that anything sent to BirdWeather is public: the station's name, pin and
the times it hears birds.

### Watching from your phone, or from anywhere

- **Same Wi-Fi:** in `setup` choose *Devices on my home network*. The page is
  then at `http://<your computer's IP>:8080` (setup shows the address). On
  Windows, allow Python through the firewall when asked.
- **From the internet with your own domain:** choose *The internet* in setup.
  Birdsong then listens on `127.0.0.1:8080` only, and you put a reverse proxy in
  front of it that provides HTTPS. With [Caddy](https://caddyserver.com/) that's
  one line in your `Caddyfile`:

  ```
  birds.example.com {
      reverse_proxy 127.0.0.1:8080
  }
  ```

  nginx:

  ```nginx
  location / {
      proxy_pass http://127.0.0.1:8080;
      proxy_set_header Host $host;
      proxy_set_header X-Forwarded-For $remote_addr;
      proxy_set_header X-Forwarded-Proto $scheme;
  }
  ```

  No domain or open ports? A free
  [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/)
  pointed at `http://localhost:8080` works too.
- **Secret path:** setup can put the page under something like
  `/k3j9x2q8mr/d8f2…/`. Anything else, including parts of that path, gets a
  plain "Not Found", so the page can't be found by guessing. The page also asks
  search engines not to index it.

## Settings

Settings live in `data/config.json`; setup writes the common ones. You can edit
the file by hand (stop Birdsong first):

| Setting | Default | Meaning |
|---|---|---|
| `min_conf` / `unusual_conf` / `vagrant_conf` | 0.70 / 0.85 / 0.95 | Confidence needed per tier |
| `sensitivity` | 1.0 | BirdNET sensitivity (higher means more detections and more mistakes) |
| `clip_days` | 30 | Delete unstarred clips after this many days |
| `max_clip_mb` | 4000 | Cap on total clip size (oldest unstarred go first) |
| `min_free_gb` | 5 | Stop saving clips below this much free disk |
| `spectro_days` | 365 | Keep spectrogram pictures this long |
| `host` / `port` / `base_path` | 127.0.0.1 / 8080 / `/` | Where the page is served |
| `behind_proxy` | false | Trust `X-Forwarded-*` headers from your reverse proxy |
| `audio_backend` / `device` | auto / default | Microphone (see `devices`) |
| `fetch_photos` | true | Download photos and descriptions from Wikipedia |
| `verify_single` | true | Shifted re-check for detections heard in one window |
| `verify_min_score` / `verify_min_windows` | 0.5 / 2 | What the re-check needs to call a detection verified |
| `human_guard_species` | five owls + Common Scoter | Species held back when human noise is in the clip |
| `human_guard_threshold` | 0.25 | BirdNET "Human non-vocal" score that counts as human noise |
| `human_guard_top_class` | true | Also count it as human noise when a Human class is BirdNET's top guess |
| `step_s` | 1.0 | Seconds between analysis windows (1.0 = 2 s overlap) |
| `species_levels` | *(empty)* | Per-species levels set with the admin slider: 1 Trusted … 5 Review all |
| `trusted_species` | *(empty)* | Shortcut list of species that start at Trusted |
| `birdweather_token` | *(empty = off)* | BirdWeather station token (set with `birdweather`) |
| `birdweather_audio` | false | Also send a FLAC clip with each detection (never clips with speech) |
| `microphone` | *(empty)* | Microphone name shown at the bottom of the page |
| `title` / `tagline` / `about` | | Text on the page |

## Privacy

- **Everything stays on your computer** in the `data/` folder: settings, the
  detections database, recordings, photos and the model.
- **Visitors can't hear recordings.** Only the admin can play them.
- **Your location** is used only to pick likely species, rounded to ~1 km, and
  never sent to the page.
- **The only connections Birdsong makes:**
  - Wikipedia / Wikimedia Commons, for bird photos and descriptions.
  - OpenStreetMap Nominatim, only during setup and only if you type a place
    name rather than coordinates.
  - From a git checkout only (release zips already include these): the
    BirdNET model from GitHub once, and uv, Python and the libraries from
    GitHub and PyPI on the first run.
  - BirdWeather, **only if you switch it on** (see above).
- **Nothing about what you hear is sent anywhere** unless you turn on
  BirdWeather sharing; even then, audio is off unless you choose it.

## Updating and uninstalling

- **Update:** download the new release zip, unzip it, and copy your old
  `data/` folder into it (or `git pull` for a git checkout). Your detections,
  recordings and settings carry over.
- **Uninstall:** `run.bat autostart remove` (if you installed it), then delete
  the folder. Nothing was installed anywhere else.

## Troubleshooting

| Problem | Fix |
|---|---|
| *"Microphone offline"* on the page | Run `devices` and `mic-test`; check the mic is plugged in and not muted; rerun `setup` to pick it again. Logs are in `data/birdsong.log`. |
| No birds after hours | Is the window shut? Try `mic-test` while you whistle or play a bird video near it. Lowering `min_conf` to 0.6 catches more birds, at the cost of more mistakes. |
| Too many wrong birds | Raise `min_conf`, hide them as admin, and check your location in `setup`. |
| Linux/Mac: `permission denied` running `./run.sh` | Your unzip tool dropped the permission. Run `sh run.sh` instead. |
| Port 8080 already in use | `run.bat --port 9000`, or change it in `setup`. |
| Linux: no audio devices | Install PortAudio (`sudo apt install libportaudio2`) or PulseAudio/PipeWire tools (`pulseaudio-utils`). Headless: `alsa-utils` gives `arecord`. |
| Admin login doesn't stick behind an HTTPS proxy | Set `behind_proxy` to `true` (setup does this when you choose *The internet*). |
| Forgot the admin password | Run `password`. |
| Intel Mac | Not supported: Google's TFLite runtime has no Intel-Mac build. |

### Without the launcher

If you'd rather use your own Python (3.10–3.13):

```sh
python -m venv .venv
.venv/bin/pip install -r requirements.txt      # Windows: .venv\Scripts\pip ...
.venv/bin/python server.py
```

## Credits and licences

- **Birdsong code:** MIT licence (see [LICENSE](LICENSE)).
- **BirdNET** model by the K. Lisa Yang Center for Conservation Bioacoustics at
  the Cornell Lab of Ornithology and Chemnitz University of Technology,
  [CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/). It
  isn't in this repository: the release zips include an unmodified copy (with
  its licence in `data/model/LICENSE-BirdNET.txt`), and a git checkout
  downloads it on first start. The model licence is **non-commercial**, so
  don't use Birdsong commercially.
- **Bird photos and descriptions** come from Wikipedia and Wikimedia Commons
  under their own licences. Each photo's author and licence is shown on the
  bird's page.
- **[Fraunces](https://github.com/undercasetype/Fraunces)** typeface, SIL Open
  Font License (`birdsong/static/fonts/OFL.txt`).
- **[uv](https://github.com/astral-sh/uv)** (Astral) provides the private
  Python environment.

See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for details.
