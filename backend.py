import json
import time
import subprocess
from dataclasses import dataclass
from typing import Any, Dict, Optional,  Iterator
import os
import threading
import signal
import requests
from cipher import SimpleStringCipher
from chat_template import Chat_templates

# =========================
# KoboldCpp backend class
# =========================

@dataclass
class KoboldCppConfig:
    base_url: str = "http://127.0.0.1:5001"
    timeout_sec: int = 180


class KoboldCppBackend:
    """
    KoboldCpp の HTTP API を叩くバックエンド。
    - generate(prompt, params) で文章生成
    - start/stop は任意（koboldcpp 実行ファイルを持っている場合のみ）
    """

    def __init__(self, config: KoboldCppConfig):
        self.temps=Chat_templates()
        self.config = config
        self._proc: Optional[subprocess.Popen] = None
        self.not_first_gen=False
        self.ssc=SimpleStringCipher("my-password")
        if os.path.exists("models/llm.json"):
            with open("models/llm.json",mode="r",encoding="utf-8")as f:
                self.models=json.load(f)
                self.model_list=json.dumps([f'"{item["urls"][0].split("/")[-1:]} : {item["urls"][0]}"' for item in self.models.values()])
        else:
            self.models=None

        if os.path.exists("gscript.json"):
            self.gscript=self.ssc.load_encrypt_json("gscript.json")
        else:
            self.gscript={}

    def check_download(self,modelname):
        path=f"models/{self.models[modelname]['urls'][0].split('/')[-1]}"
        def downloading(path:str,download_path:str):
            with requests.get(path,stream=True) as r:
                    r.raise_for_status()
                    with open(download_path.replace(".gguf",".part"),"wb") as f:
                        for chunk in r.iter_content(chunk_size=1024*1024):
                            if chunk:
                                f.write(chunk)
                    os.replace(download_path.replace(".gguf",".part"),download_path.replace(".part",".gguf"))
        if os.path.exists(path):
            return True,path
        else:
            threading.Thread(target=downloading,args=(self.models[modelname]['urls'][0],path,),daemon=True).start()
            return False,path
    

    # ---- HTTP helpers ----
    def _post_json(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        url = self.config.base_url.rstrip("/") + path
        r = requests.post(url, json=payload, timeout=self.config.timeout_sec)
        r.raise_for_status()
        return r.json()
    
    def _get_none(self,path: str):
        url = self.config.base_url.rstrip("/") + path
        r = requests.get(url, timeout=self.config.timeout_sec)
        r.raise_for_status()
        return r.json()

    def _try_generate_endpoints(self, payload: Dict[str, Any]) -> str:
        """
        KoboldCpp は環境によって返却形式が微妙に違うので、代表的な候補を試す。
        """
        candidates = [
            "/api/v1/generate",         # Kobold / KoboldCpp 互換でよく見る
            "/api/v1/generate/text",    # 亜種
            "/api/generate",            # 旧/簡易
        ]

        last_err: Optional[Exception] = None
        for p in candidates:
            try:
                data = self._post_json(p, payload)
                # 返却形式候補を吸収
                # 例1: {"results":[{"text":"..."}]}
                if isinstance(data, dict) and "results" in data and data["results"]:
                    item = data["results"][0]
                    if isinstance(item, dict) and "text" in item:
                        return str(item["text"])

                # 例2: {"text":"..."}
                if isinstance(data, dict) and "text" in data:
                    return str(data["text"])

                # 例3: {"data":{"text":"..."}}
                if isinstance(data, dict) and "data" in data and isinstance(data["data"], dict) and "text" in data["data"]:
                    return str(data["data"]["text"])

                # 形式が想定外ならダンプしてエラー扱い
                raise RuntimeError(f"未知のレスポンス形式: {json.dumps(data, ensure_ascii=False)[:400]}")

            except Exception as e:
                last_err = e

        raise RuntimeError(f"生成APIに接続できませんでした。base_url={self.config.base_url} / err={last_err}")

    # ---- Public API ----
    def generate(self, prompt: str, params: Dict[str, Any]) -> str:
        """
        prompt: 入力プロンプト
        params: temperature, top_k, top_p, repeat_penalty, max_length など
        """
        # Kobold系の一般的な payload 名に寄せる
        payload = {
            "prompt": prompt,
            "temperature": float(params.get("temperature", 0.7)),
            "top_k": int(params.get("top_k", 40)),
            "top_p": float(params.get("top_p", 0.95)),
            "rep_pen": float(params.get("repeat_penalty", 1.1)),
            # Koboldは max_length / max_context_length などが混在しがち
            "max_length": int(params.get("max_new_tokens", 400)),
        }
        return self._try_generate_endpoints(payload)
    
    def _extract_text_from_generate_resp(self, data: dict) -> str:
            if isinstance(data, dict) and "results" in data and data["results"]:
                return str(data["results"][0].get("text", ""))
            if isinstance(data, dict) and "text" in data:
                return str(data["text"])
            if isinstance(data, dict) and "data" in data and isinstance(data["data"], dict) and "text" in data["data"]:
                return str(data["data"]["text"])
            return ""

    def generate_polled_stream(self, prompt: str, params: Dict) -> Iterator[str]:
        """
        1) 別スレッドで /api/v1/generate を投げて生成開始（ブロッキング回避）
        2) 生成中に /api/extra/generate/check をポーリングして増分を yield
        3) 生成スレッド終了でループ終了（リトライが詰まらない）
        """
        modelname=self._get_none("/api/v1/model")["result"]
        print(modelname)
        template=self.temps.templates["chatml"]
        for temp in self.temps.temp_name.keys():
            if temp in modelname:
                template=self.temps.templates[self.temps.temp_name[temp]]

        payload = {
            "prompt": template.format(prompt),
            "temperature": float(params.get("temperature", 0.7)),
            "top_k": int(params.get("top_k", 40)),
            "top_p": float(params.get("top_p", 0.95)),
            "rep_pen": float(params.get("repeat_penalty", 1.1)),
            "max_length": int(params.get("max_new_tokens", 400)),
        }

        done = {"flag": False}
        final = {"text": "", "err": None}

        def _run_generate():
            try:
                data = self._post_json("/api/v1/generate", payload)
                final["text"] = self._extract_text_from_generate_resp(data)
            except Exception as e:
                final["err"] = e
            finally:
                done["flag"] = True

        t = threading.Thread(target=_run_generate, daemon=True)
        t.start()

        emitted = ""
        idle_count = 0

        while not done["flag"]:
            try:
                chk = self._post_json("/api/extra/generate/check", {})
                cur = ""
                # よくある形式: {"results":[{"text":"..."}]}
                if isinstance(chk, dict) and "results" in chk and chk["results"]:
                    cur = str(chk["results"][0].get("text", "") or "")
                elif isinstance(chk, dict) and "text" in chk:
                    cur = str(chk["text"] or "")

                if cur.startswith(emitted):
                    delta = cur[len(emitted):]
                else:
                    delta = cur  # 形式が変わった/巻き戻った場合は全体出し

                if delta:
                    emitted = cur
                    idle_count = 0
                    yield delta
                else:
                    idle_count += 1

                # check が機能してない環境で永久待ちにならない保険
                if idle_count > 200:  # 0.25秒 * 200 = 約50秒 無変化
                    break

            except Exception:
                # check が無い / 404 / 一時エラーでも、生成スレッドが終われば抜ける
                pass

            time.sleep(0.02)

        # スレッド完了待ち（短く）
        t.join(timeout=0.5)

        if final["err"] is not None:
            raise RuntimeError(str(final["err"]))

        #最後に取りこぼしがあれば吐く
        if final["text"].startswith(emitted):
            tail = final["text"][len(emitted):]
            if tail:
                yield tail
        else:
            # 念のため全出し
            if final["text"]:
                yield final["text"]

        

    def abort(self) -> None:
        """
        生成中断（対応している場合のみ）
        """
        for p in ["/api/v1/abort", "/api/abort"]:
            try:
                self._post_json(p, {})
                return
            except Exception:
                pass

    # ---- Optional: start/stop koboldcpp process ----
    def start(self, koboldcpp_exe: str, model_path: str, layers: int = 40, port: int = 5001, context_length: int = 2048):
        """
        koboldcpp をプロセス起動したい場合用（任意）。
        Linux: ./koboldcpp を同ディレクトリに置いて実行 (chmod +x 済み)
        Windows: koboldcpp.exe を実行
        """
        if self._proc and self._proc.poll() is None:
            return "すでに起動しています。"

        # 実行ファイルの解決（同ディレクトリの "koboldcpp" を想定）
        exe = koboldcpp_exe.strip() if koboldcpp_exe else "koboldcpp"

        # 相対パスの場合は "./" を付けた方が確実（LinuxでPATHに無いことが多い）
        if not os.path.isabs(exe) and os.path.exists(exe):
            # "koboldcpp" が同階層にあるケース
            if os.name != "nt" and not exe.startswith("./"):
                exe = "./" + exe

        cmd = [
            exe,
            "--model", f"models/{self.models[model_path]['urls'][0].split('/')[-1]}",
            "--port", str(port),
            "--gpulayers", str(layers),
            "--contextsize", str(context_length),
        ]

        # OS 別にプロセスグループ（セッション）を分けて、stop() でまとめて落とせるようにする
        popen_kwargs = dict(text=True, bufsize=1)

        if os.name == "nt":
            # Windows: 新しいプロセスグループ
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            # Linux/macOS: 新しいセッション
            # Python 3.2+ なら start_new_session=True が一番安全
            popen_kwargs["start_new_session"] = True

        self._proc = subprocess.Popen(cmd, **popen_kwargs)
        self.not_first_gen = False

        return f"起動コマンド: {' '.join(cmd)}"

    def stop(self) -> str:
        if not self._proc:
            return "起動していません。"

        if self._proc.poll() is None:
            try:
                if os.name == "nt":
                    # Windows: CTRL_BREAK → terminate → kill
                    try:
                        self._proc.send_signal(signal.CTRL_BREAK_EVENT)
                    except Exception:
                        pass
                    self._proc.terminate()
                else:
                    # Linux/macOS: セッション/プロセスグループに SIGTERM
                    # start_new_session=True の場合、pid が pgid になっているので killpg が効く
                    try:
                        os.killpg(self._proc.pid, signal.SIGTERM)
                    except Exception:
                        # killpg が効かない場合は通常 terminate
                        self._proc.terminate()

                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                # だめなら強制 kill
                try:
                    if os.name == "nt":
                        self._proc.kill()
                    else:
                        try:
                            os.killpg(self._proc.pid, signal.SIGKILL)
                        except Exception:
                            self._proc.kill()
                except Exception:
                    pass

        self._proc = None
        return "終了しました。"

    def reload_gscript(self,path: str):
        if os.path.exists(path):
            self.gscript=self.ssc.load_encrypt_json(path)