# Third-party notices

Birdsong's own code is MIT-licensed. It relies on the following, none of
which is relicensed by Birdsong.

## BirdNET (included in release zips; downloaded on first start from a git checkout)

BirdNET V2.4 model and labels, by Stefan Kahl, Connor M. Wood, Maximilian
Eibl and Holger Klinck (K. Lisa Yang Center for Conservation Bioacoustics,
Cornell Lab of Ornithology; Chemnitz University of Technology).
Licence: Creative Commons Attribution-NonCommercial-ShareAlike 4.0
International (CC BY-NC-SA 4.0). Source:
https://github.com/birdnet-team/BirdNET-Analyzer

> Kahl, S., Wood, C. M., Eibl, M., & Klinck, H. (2021). BirdNET: A deep
> learning solution for avian diversity monitoring. Ecological Informatics,
> 61, 101236.

Release zips include unmodified copies of the model and label files in
`data/model/`, together with `LICENSE-BirdNET.txt` (attribution and licence).
The files are not in this git repository.

Because of this licence, Birdsong as a whole should only be used
non-commercially.

## Fraunces typeface (bundled: birdsong/static/fonts)

Copyright 2018 The Fraunces Project Authors. SIL Open Font License 1.1, full
text in `birdsong/static/fonts/OFL.txt`.

## Wikipedia and Wikimedia Commons (fetched at runtime)

Species descriptions are fetched from Wikipedia (CC BY-SA). Photos are the
lead image of each species' Wikipedia article, fetched from Wikimedia Commons
under each file's own licence. The author and licence are recorded with each
photo and shown on the bird's page with a link to the file.

## Python and Python packages (bundled in release zips; installed by the launcher from a git checkout)

- numpy (BSD-3-Clause)
- ai-edge-litert, Google's TensorFlow Lite runtime (Apache-2.0)
- sounddevice (MIT), which bundles PortAudio (MIT) on Windows and macOS
- soundfile (BSD-3-Clause), which bundles libsndfile (LGPL-2.1), plus LAME and
  mpg123 for MP3. These are unmodified shared libraries from the soundfile
  wheel on PyPI; their sources are available from libsndfile.github.io,
  lame.sourceforge.io and mpg123.de.
- CPython 3.12 (PSF License), the relocatable python-build-standalone builds
  (https://github.com/astral-sh/python-build-standalone), with each bundled
  package's own licence files in `runtime/` (`site-packages/*.dist-info/`).

## uv (downloaded by the launcher from a git checkout only)

Astral's uv (MIT / Apache-2.0) manages the private Python environment.
