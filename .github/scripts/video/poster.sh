#!/bin/sh
# Draws the README's poster for the launch video: one frame of the narrated
# tour (the Spend chart with its tooltip, shot 3), cropped above the burned-in
# subtitle, with a round play glyph on top.
#
#   .github/scripts/video/poster.sh VIDEO.mp4 OUT.png [SECONDS]
#
# The video is made in its own project from the same synthetic logs; only
# ffmpeg is needed here.
set -eu

video=$1
out=$2
at=${3:-16}

# The frame: 1760 x 990 (16:9) from the 1920 x 1080 video, the browser window
# whole and the subtitle line below it left out, scaled to 1280 x 720.
# The glyph: a dark disk 120 px across with a white triangle, drawn with geq on
# a transparent layer and laid over the center.
ffmpeg -loglevel error -y -ss "$at" -i "$video" \
  -f lavfi -i "color=c=black@0:s=1280x720,format=rgba" \
  -filter_complex "
    [0:v]crop=1760:990:80:0,scale=1280:720:flags=lanczos,format=rgba[bg];
    [1:v]geq=
      r='255*(between(X,620,686)*lte(abs(Y-360),(686-X)*0.58)+between(hypot(X-640,Y-360),60,63))':g='255*(between(X,620,686)*lte(abs(Y-360),(686-X)*0.58)+between(hypot(X-640,Y-360),60,63))':b='255*(between(X,620,686)*lte(abs(Y-360),(686-X)*0.58)+between(hypot(X-640,Y-360),60,63))':
      a='if(between(X,620,686)*lte(abs(Y-360),(686-X)*0.58),255,if(lte(hypot(X-640,Y-360),60),165,if(lte(hypot(X-640,Y-360),63),220,0)))'
    [glyph];
    [bg][glyph]overlay=0:0:format=auto,format=rgb24" \
  -frames:v 1 "$out"
