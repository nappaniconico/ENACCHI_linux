import json
import subprocess
from dataclasses import dataclass
from typing import Any, Dict, Optional, Iterator
import os
import threading
import signal
import requests

from cipher import SimpleStringCipher

# =========================
# llama.cpp (llama-server) backend class
# =========================

@dataclass
class LlamaCppConfig:
    """
    llama-server の OpenAI互換APIを叩く設定。
    例: base_url="http://127.0.0.1:5001"
    """
    base_url: str = "http://127.0.0.1:5001"
    timeout_sec: int = 180


class LlamaCppBackend:
    """
    llama.cpp の llama-server (OpenAI互換API) を叩くバックエンド。

    - generate(prompt, params) -> str
    - generate_stream(prompt, params) -> Iterator[str]  (SSEストリーミング)
    - start/stop は任意（llama-server を起動したい場合）
    """

    def __init__(self, config: LlamaCppConfig):
        self.config = config
        self._proc: Optional[subprocess.Popen] = None

        # 既存の仕組み（モデル一覧・ガタライズスクリプト）はそのまま流用
        self.ssc = SimpleStringCipher("my-password")

        if os.path.exists("models/llm.json"):
            with open("models/llm.json", mode="r", encoding="utf-8") as f:
                self.models = json.load(f)
        else:
            self.models = None

        if os.path.exists("gscript.json"):
            self.gscript = self.ssc.load_encrypt_json("gscript.json")
        else:
            self.gscript = {}

    # ---- model download helper (unchanged) ----
    def check_download(self, modelname: str):
        path = f"models/{self.models[modelname]['urls'][0].split('/')[-1]}"

        def downloading(url: str, download_path: str):
            with requests.get(url, stream=True) as r:
                r.raise_for_status()
                with open(download_path.replace(".gguf", ".part"), "wb") as f:
                    for chunk in r.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            f.write(chunk)
                os.replace(download_path.replace(".gguf", ".part"), download_path.replace(".part", ".gguf"))

        if os.path.exists(path):
            return True, path
        else:
            threading.Thread(target=downloading, args=(self.models[modelname]["urls"][0], path), daemon=True).start()
            return False, path

    # ---- HTTP helpers ----
    def _post_json(self, path: str, payload: Dict[str, Any], stream: bool = False):
        url = self.config.base_url.rstrip("/") + path
        return requests.post(url, json=payload, timeout=self.config.timeout_sec, stream=stream)

    def _extract_text_from_openai_completion(self, data: dict) -> str:
        # OpenAI互換の /v1/completions 形式
        # {"choices":[{"text":"..."}], ...}
        if isinstance(data, dict) and "choices" in data and data["choices"]:
            ch0 = data["choices"][0]
            if isinstance(ch0, dict) and "text" in ch0:
                return str(ch0["text"])
        return ""

    # ---- Public API ----
    def generate(self, prompt: str, params: Dict[str, Any]) -> str:
        """
        llama-server の /v1/completions を利用。
        UI側はすでに「プロンプト文字列」を作っているので Chat ではなく Completions で扱う。
        """
        payload = {
            "prompt": prompt,
            "max_tokens": int(params.get("max_new_tokens", 400)),
            "temperature": float(params.get("temperature", 0.7)),
            "top_p": float(params.get("top_p", 0.95)),
            # llama.cpp は OpenAI互換で top_k / repeat_penalty 等も受け付けるビルドが多い
            "top_k": int(params.get("top_k", 40)),
            "repeat_penalty": float(params.get("repeat_penalty", 1.1)),
            "stream": False,
        }

        r = self._post_json("/v1/completions", payload, stream=False)
        r.raise_for_status()
        data = r.json()
        text = self._extract_text_from_openai_completion(data)
        if text == "":
            raise RuntimeError(f"未知のレスポンス形式: {json.dumps(data, ensure_ascii=False)[:400]}")
        return text

    def generate_stream(self, prompt: str, params: Dict[str, Any]) -> Iterator[str]:
        """
        llama-server の /v1/completions (stream=True) をSSEとしてパースして増分を yield。
        """
        payload = {
            "prompt": prompt,
            "max_tokens": int(params.get("max_new_tokens", 400)),
            "temperature": float(params.get("temperature", 0.7)),
            "top_p": float(params.get("top_p", 0.95)),
            "top_k": int(params.get("top_k", 40)),
            "repeat_penalty": float(params.get("repeat_penalty", 1.1)),
            "stream": True,
        }

        r = self._post_json("/v1/completions", payload, stream=True)
        r.raise_for_status()
        r.encoding="utf-8"

        # SSE: "data: {...}\n\n" が流れてくる想定
        for raw in r.iter_lines(decode_unicode=True):
            if not raw:
                continue
            line = raw.strip()
            if not line.startswith("data:"):
                continue
            data_str = line[len("data:"):].strip()
            if data_str == "[DONE]":
                break
            try:
                obj = json.loads(data_str)
            except Exception:
                continue

            delta = self._extract_text_from_openai_completion(obj)
            if delta:
                yield delta

    def abort(self) -> None:
        """
        llama-server 側には Kobold の /abort 相当が無いことが多いので no-op。
        """
        return

    # ---- Optional: start/stop llama-server process ----
    def start(
        self,
        llama_server_exe: str,
        model_key: str,
        layers: int = 0,
        host: str = "127.0.0.1",
        port: int = 5001,
        context_length: int = 2048,
        extra_args: Optional[list] = None,
    ) -> str:
        """
        llama-server をプロセス起動したい場合用（任意）。

        llama_server_exe: 例: "llama-server" もしくは "/path/to/llama-server"
        model_key: models/llm.json のキー（UIのドロップダウンの値）
        layers: GPUレイヤ数（0でCPUのみ）
        """
        if self._proc and self._proc.poll() is None:
            return "すでに起動しています。"

        if not self.models:
            raise RuntimeError("models/llm.json が見つからないため、モデル選択起動ができません。")

        model_path = f"models/{self.models[model_key]['urls'][0].split('/')[-1]}"

        cmd = [
            llama_server_exe,
            "-m", model_path,
            "--host", host,
            "--port", str(port),
            "-c", str(context_length),
            "-ngl", str(layers),
        ]
        if extra_args:
            cmd.extend(extra_args)

        popen_kwargs = dict(text=True, bufsize=1)

        if os.name == "nt":
            # Windows: CREATE_NEW_PROCESS_GROUP でCTRL_BREAKを送れるようにする
            popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        else:
            # POSIX: 新しいプロセスグループを作る
            popen_kwargs["preexec_fn"] = os.setsid

        self._proc = subprocess.Popen(cmd, **popen_kwargs)
        self.config.base_url = f"http://{host}:{port}"
        return f"起動コマンド: {' '.join(cmd)}"

    def stop(self) -> str:
        if not self._proc:
            return "起動していません。"

        try:
            if self._proc.poll() is None:
                if os.name == "nt":
                    try:
                        self._proc.send_signal(signal.CTRL_BREAK_EVENT)
                    except Exception:
                        pass
                    self._proc.terminate()
                    try:
                        self._proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        self._proc.kill()
                else:
                    try:
                        os.killpg(self._proc.pid, signal.SIGTERM)
                    except Exception:
                        self._proc.terminate()
                    try:
                        self._proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        try:
                            os.killpg(self._proc.pid, signal.SIGKILL)
                        except Exception:
                            self._proc.kill()
        finally:
            self._proc = None

        return "終了しました。"

    def reload_gscript(self, path: str):
        if os.path.exists(path):
            self.gscript = self.ssc.load_encrypt_json(path)
