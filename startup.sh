#!/usr/bin/env bash
# Azure App Service (Linux, Python) startup command.
#
# ffmpeg now comes from the imageio-ffmpeg wheel (see requirements.txt), so
# there's nothing to install here. Installing it with apt-get on every boot was
# slow and could fail outright, which took the whole container down with it.
#
# --threads matters: the page polls /progress while a download is still running,
# and a single-threaded worker would queue those polls behind it.
exec gunicorn --bind=0.0.0.0:${PORT:-8000} --timeout 1800 --workers 1 --threads 8 server:app
