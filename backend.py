import json
import time
import subprocess
from dataclasses import dataclass
from typing import Any, Dict, Optional,  Iterator
import os
import threading
import signal
import requests
import glob
import socket
from cipher import SimpleStringCipher
from chat_template import Chat_templates

# =========================
# KoboldCpp backend class
# =========================

@dataclass
class KoboldCppConfig:
    base_url: str = "http://127.0.0.1:5001"
    timeout_sec: int = 180
    kobold_path: str = "koboldcpp"


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
        self.comp_proc: Optional[subprocess.Popen] = None
        self.not_first_gen=False
        self.ssc=SimpleStringCipher("my-password")
        if os.path.exists("models/llm.json"):
            with open("models/llm.json",mode="r",encoding="utf-8")as f:
                self.models=json.load(f)
            for key in list(self.models.keys()):
                if "オリジナル" in key:
                    self.models.pop(key)
            modelfiles=glob.glob("models/*.gguf")
            known_files=[os.path.basename(item["urls"][0]) for item in self.models.values()]
            for item in modelfiles:
                new_modelname=os.path.basename(item)
                if new_modelname not in known_files:
                    self.models[f"オリジナル/{new_modelname.replace('.gguf','')}"]={
                        "max_gpu_layer":0,
                        "context_size":4096,
                        "urls":[new_modelname],
                    }
            self.model_list=json.dumps([f'"{os.path.basename(item["urls"][0])} : {item["urls"][0]}"' for item in self.models.values()])
        else:
            self.models=None

        if os.path.exists("gscript.json"):
            self.gscript=self.ssc.load_encrypt_json("gscript.json")
        else:
            self.gscript={}

    def check_download(self,modelname):
        model_url=self.models[modelname]["urls"][0]
        path=os.path.join("models", os.path.basename(model_url))
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
        elif not model_url.startswith(("http://", "https://")):
            return False,path
        else:
            threading.Thread(target=downloading,args=(model_url,path,),daemon=True).start()
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

    def generate_polled_stream(self, prompt: str, params: Dict, header: str="", current_text: str="", cut_mode: str="シンプル", exepath: str="koboldcpp", max_tokens: int=1024) -> Iterator[str]:
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

        formated=self.comp_hub(cut_mode, header, current_text, template, exepath, max_tokens)
        if self.check_over_tokens(formated)+max_tokens>0:
            yield "Over Max Tokens"

        payload = {
            "prompt": formated if current_text else template.format(prompt),
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
    def _resolve_exe(self, koboldcpp_exe: str) -> str:
        exe = koboldcpp_exe.strip() if koboldcpp_exe else "koboldcpp.exe" if os.name == "nt" else "koboldcpp"
        if not os.path.isabs(exe) and os.path.exists(exe) and os.name != "nt" and not exe.startswith("./"):
            exe = "./" + exe
        return exe

    def _popen(self, cmd: list[str]) -> subprocess.Popen:
        popen_kwargs = dict(text=True, bufsize=1)
        if os.name == "nt":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True
        return subprocess.Popen(cmd, **popen_kwargs)

    def _stop_proc(self, proc: subprocess.Popen) -> None:
        if proc.poll() is not None:
            return
        try:
            if os.name == "nt":
                try:
                    proc.send_signal(signal.CTRL_BREAK_EVENT)
                except Exception:
                    pass
                proc.terminate()
            else:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except Exception:
                    proc.terminate()
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                proc.kill()
            else:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except Exception:
                    proc.kill()

    def start(self, koboldcpp_exe: str, model_path: str, layers: int = 40, port: int = 5001, context_length: int = 2048):
        """
        koboldcpp をプロセス起動したい場合用（任意）。
        Linux: ./koboldcpp を同ディレクトリに置いて実行 (chmod +x 済み)
        Windows: koboldcpp.exe を実行
        """
        if self._proc and self._proc.poll() is None:
            return "すでに起動しています。"

        exe = self._resolve_exe(koboldcpp_exe)

        cmd = [
            exe,
            "--model", os.path.join("models", os.path.basename(self.models[model_path]["urls"][0])),
            "--port", str(port),
            "--gpulayers", str(layers),
            "--contextsize", str(context_length),
        ]

        self._proc = self._popen(cmd)
        self.not_first_gen = False

        return f"起動コマンド: {' '.join(cmd)}"

    def stop(self) -> str:
        if not self._proc:
            return "起動していません。"

        if self._proc.poll() is None:
            self._stop_proc(self._proc)

        self._proc = None
        return "終了しました。"

    def reload_gscript(self,path: str):
        if os.path.exists(path):
            self.gscript=self.ssc.load_encrypt_json(path)

    # ---- Context compression ----
    def setting_aicompresser(self, exepath: str):
        model_path=os.path.join("models", "LFM2.5-1.2B-JP-Q8_0.gguf")

        def downloading(path:str,download_path:str):
            with requests.get(path,stream=True) as r:
                    r.raise_for_status()
                    with open(download_path.replace(".gguf",".part"),"wb") as f:
                        for chunk in r.iter_content(chunk_size=1024*1024):
                            if chunk:
                                f.write(chunk)
                    os.replace(download_path.replace(".gguf",".part"),download_path)

        if not os.path.exists(model_path):
            result=threading.Thread(
                target=downloading,
                args=("https://huggingface.co/LiquidAI/LFM2.5-1.2B-JP-GGUF/resolve/main/LFM2.5-1.2B-JP-Q8_0.gguf?download=true", model_path),
                daemon=True,
            )
            result.start()
            result.join(timeout=300)

        if self.comp_proc and self.comp_proc.poll() is None:
            return "すでに起動しています。"

        exe=self._resolve_exe(exepath)
        if not os.path.exists(exe.replace("./", "", 1)) and not os.path.exists(exe):
            return f"{exe} が見つかりません"

        cmd = [
            exe,
            "--model", model_path,
            "--port", "5015",
            "--gpulayers", "0",
            "--contextsize", "2048",
        ]
        self.comp_proc = self._popen(cmd)
        return "起動完了"

    def stop_aicompesser(self):
        if not self.comp_proc:
            return "起動していません。"
        if self.comp_proc.poll() is None:
            self._stop_proc(self.comp_proc)
        self.comp_proc = None
        return "終了しました。"

    def send_aicompresser(self,text: str):
        payload = {
            "prompt": "以下の文章を3文以内で要約してください。\n"+text,
            "temperature": 0.7,
            "top_k": 40,
            "top_p": 0.95,
            "rep_pen": 1.1,
            "max_length": 256,
        }

        candidates = [
            "/api/v1/generate",
            "/api/v1/generate/text",
            "/api/generate",
        ]

        last_err: Optional[Exception] = None
        for p in candidates:
            try:
                url = "http://127.0.0.1:5015"+p
                r = requests.post(url, json=payload, timeout=self.config.timeout_sec)
                r.raise_for_status()
                data = r.json()
                text_result = self._extract_text_from_generate_resp(data)
                if text_result:
                    return text_result
                raise RuntimeError(f"未知のレスポンス形式: {json.dumps(data, ensure_ascii=False)[:400]}")
            except Exception as e:
                last_err = e

        raise RuntimeError(f"生成APIに接続できませんでした。base_url=http://127.0.0.1:5015 / err={last_err}")

    def check_over_tokens(self,text: str):
        token_values=int(self._post_json("/api/extra/tokencount",{"prompt":text})["value"])
        true_max_context_length=int(self._get_none("/api/extra/true_max_context_length")["value"])
        print(f"check current token {token_values}/{true_max_context_length}")
        return token_values-true_max_context_length

    def check_current_token(self,text: str):
        return int(self._post_json("/api/extra/tokencount",{"prompt":text})["value"])

    def simple_compresser(self,texts:list[str], header: str, template: str, max_tokens: int):
        over=True
        formated=template.format(header + "\n".join(texts))
        over_length=self.check_over_tokens(formated)+max_tokens
        current_length=max(self.check_current_token("\n".join(texts)), 1)
        first_sentence=max(int(len(texts)*over_length/current_length), 0)
        while over and first_sentence < len(texts):
            new_texts=texts[first_sentence:]
            if self.check_over_tokens(template.format(header + "\n".join(new_texts)))+max_tokens<0:
                over=False
            else:
                first_sentence+=1
        return "\n".join(texts[first_sentence:])

    def ai_compresser(self,texts:list[str], header: str, template: str, max_tokens: int):
        n = 20
        chunks = [texts[i:i + n] for i in range(0, len(texts), n)]
        if self.comp_proc and self.comp_proc.poll() is None:
            pass
        else:
            print(self.setting_aicompresser(exepath=self.config.kobold_path))
            def is_listening(host: str="127.0.0.1",port: int = 5015, timeout: float =0.3)-> bool:
                    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                        s.settimeout(timeout)
                        return s.connect_ex((host, port)) == 0
            deadline=time.time()+300
            while not is_listening() and time.time()<deadline:
                time.sleep(0.1)
        over=True
        current_index=0
        comped_chunks=[]
        while over and current_index < len(chunks):
            comped_chunks.append(self.send_aicompresser("\n".join(chunks[current_index])))
            new_raw_text=""
            for item in comped_chunks:
                new_raw_text+=item
            for item in chunks[len(comped_chunks):]:
                new_raw_text+="\n".join(item)
            new_texts=template.format(header + new_raw_text)
            length=self.check_over_tokens(new_texts)
            if length+max_tokens<0:
                over=False
            current_index+=1
        return new_raw_text

    def comp_hub(self,mode: str,header: str, current_text:str, template: str,exepath: str, max_tokens: int):
        formatted=template.format(header+current_text)
        if self.check_over_tokens(formatted)+max_tokens<0:
            return formatted
        texts=current_text.split("\n")
        mode_dict={
            "シンプル":1,
            "AI圧縮":2,
        }
        match mode_dict.get(mode, 1):
            case 1:
                self.stop_aicompesser()
                result=self.simple_compresser(texts,header,template,max_tokens)
            case 2:
                result=self.ai_compresser(texts,header,template,max_tokens)
            case _:
                result=""
        return template.format(header+result)
