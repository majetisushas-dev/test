"""Run once in the voice container's shell; downloads weights to its mounted disk.

No downloads occur at application startup. Review licenses before running this.
Ollama runs separately and is not installed by this script.
"""
import os
import subprocess
import sys
from pathlib import Path

from huggingface_hub import snapshot_download

stt_dir = Path(os.getenv('STT_MODEL_PATH', '/data/models/faster-whisper-small'))
voice_file = Path(os.getenv('PIPER_MODEL_PATH', '/data/models/piper/en_US-lessac-medium.onnx'))
stt_dir.mkdir(parents=True, exist_ok=True)
voice_file.parent.mkdir(parents=True, exist_ok=True)
snapshot_download('Systran/faster-whisper-small', local_dir=str(stt_dir))
subprocess.run([sys.executable, '-m', 'piper.download_voices', voice_file.stem,
                '--download-dir', str(voice_file.parent)], check=True)
if not voice_file.is_file() or not Path(str(voice_file) + '.json').is_file():
    raise RuntimeError('Piper did not produce the configured ONNX voice and JSON config')
print('Speech model downloads complete. Refresh Integrations to check readiness.')
