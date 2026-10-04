# PyInstaller spec for the desktop build.
#
# Build with:  pyinstaller ytdownloader.spec
# Output:      dist/YTDownloader.exe  (single file, no Python install needed)
#
# ffmpeg is bundled explicitly at ffmpeg/ffmpeg.exe rather than relying on
# imageio_ffmpeg's own resource lookup, which isn't guaranteed to survive
# being frozen — find_ffmpeg() in server.py checks this exact path first.
import imageio_ffmpeg

block_cipher = None

a = Analysis(
    ['desktop.py'],
    pathex=[],
    binaries=[(imageio_ffmpeg.get_ffmpeg_exe(), 'ffmpeg')],
    datas=[('index.html', '.')],
    hiddenimports=['yt_dlp', 'truststore'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='YTDownloader',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # no terminal window — this is a desktop app
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
