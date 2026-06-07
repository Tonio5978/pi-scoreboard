#!/usr/bin/env python3
"""Solid-color diagnostic for the 2-panel chain (128x32).

Fills the WHOLE display with one pure color at a time so we can tell whether
each physical panel renders each channel correctly. Watch BOTH panels.

Usage:
    sudo -E ./bin/python rgbtest.py [gpio_slowdown] [brightness]

  gpio_slowdown : data clock speed. Higher = slower/cleaner signal. Try 1..5.
  brightness    : 1..100, default 40. Lower = less current draw.

Power test: if WHITE looks red on a panel at brightness 40 but turns white
at a low brightness (e.g. 15), that panel is power-starved, not broken.

Examples:
    sudo -E ./bin/python rgbtest.py 4 40
    sudo -E ./bin/python rgbtest.py 4 15

Ctrl-C to quit.
"""
import sys
import time
from rgbmatrix import RGBMatrix, RGBMatrixOptions

slowdown = int(sys.argv[1]) if len(sys.argv) > 1 else 4
brightness = int(sys.argv[2]) if len(sys.argv) > 2 else 40

options = RGBMatrixOptions()
options.rows = 32
options.cols = 64
options.chain_length = 2
options.gpio_slowdown = slowdown
options.brightness = brightness

print(f"gpio_slowdown={slowdown}  brightness={brightness}\n")

matrix = RGBMatrix(options=options)
canvas = matrix.CreateFrameCanvas()

frames = [
    ("RED    (both panels should be red)",   (255, 0, 0)),
    ("GREEN  (both panels should be green)", (0, 255, 0)),
    ("BLUE   (both panels should be blue)",  (0, 0, 255)),
    ("WHITE  (both panels should be white)", (255, 255, 255)),
]

print("Watch both panels. Each color holds 4s.\n")
try:
    while True:
        for name, (r, g, b) in frames:
            print("Showing:", name)
            canvas.Fill(r, g, b)
            canvas = matrix.SwapOnVSync(canvas)
            time.sleep(4)
except KeyboardInterrupt:
    matrix.Clear()
    print("\nbye")
