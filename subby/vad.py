# -*- coding: utf-8 -*-
"""Silero VAD a faster-whisper saját ONNX modelljével, 32 ms-onként, állapottal."""

import os

import numpy as np

FRAME = 512                      # a Silero VAD ablakmérete: 32 ms 16 kHz-en
FRAME_S = FRAME / 16000


def ms2frames(ms):
    return max(1, int(round(ms / 1000.0 / FRAME_S)))


class StreamingVAD:
    def __init__(self):
        import faster_whisper
        import onnxruntime

        path = os.path.join(os.path.dirname(faster_whisper.__file__), "assets", "silero_vad_v6.onnx")
        if not os.path.exists(path):
            raise RuntimeError(
                "nem találom a faster-whisper VAD modelljét (silero_vad_v6.onnx); "
                "frissíts: pip install -U faster-whisper (legalább 1.2 kell)"
            )
        opts = onnxruntime.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        opts.log_severity_level = 4
        self.sess = onnxruntime.InferenceSession(path, providers=["CPUExecutionProvider"], sess_options=opts)
        self.h = np.zeros((1, 1, 128), np.float32)
        self.c = np.zeros((1, 1, 128), np.float32)
        self.ctx = np.zeros((1, 64), np.float32)

    def __call__(self, frame):
        x = np.concatenate([self.ctx, frame[None, :]], axis=1)
        out, self.h, self.c = self.sess.run(None, {"input": x, "h": self.h, "c": self.c})
        self.ctx = x[:, -64:]
        return float(out.reshape(-1)[0])
