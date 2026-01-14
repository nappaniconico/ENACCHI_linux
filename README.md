# Easy Novel Assistant osuChitsu Linux  

llama.cpp を使って小説生成を行う Gradio アプリです。  
特定のコミュニティ向けに調整されたLLMモデルを対象として作成されており、プロンプト補助、パラメータ調整、出力保存、ガタライズスクリプト編集を備えています。

## 動作環境
- Linux(Ubuntu24.04)
- Python 3.11 以上
- NVIDIA GeForce RTX 20 / 30 / 40 番台で動作確認済み

## インストール  
### ワンステップインストール (おすすめ)  
  

### マニュアルインストール 
自身でのPythonまたはuvのインストール、gitのインストールが可能な場合は以下のいずれかの手順でもインストール可能です。  

#### venv を使う場合  
1. Pythonがインストール済みであることを確認する
2. アプリを配置したいフォルダでコマンドプロンプトを起動し、`git clone https://github.com/nappaniconico/EasyNovelAssistantosuChitsu.git`を実行する


#### uv を使う場合 (おすすめ)  
1. Pythonがインストール済みであることを確認する
2. アプリを配置したいフォルダでコマンドプロンプトを起動し、`git clone https://github.com/nappaniconico/EasyNovelAssistantosuChitsu.git`を実行する
 

起動
----
`launch.`をクリックして実行  
しばらくすると自動的にブラウザが起動します。

基本的な使い方
--------------
1. 右側の「構成」タブで、タイトル/ジャンル/登場人物/舞台背景などを入力します。
2. 「パラメータ」タブで生成パラメータを調整します。
3. 「llama-server」タブで `llama-server` のパスを指定して「起動」します。外部で起動済みの場合は `base_url` を合わせて「起動」します。
4. 「リトライ」を押して生成します。出力は左のテキストボックスに表示されます。

メモ
----
- モデルは `models/llm.json` に定義されています。未ダウンロードの場合は起動時に自動取得します。
- 生成結果は「保存/終了」タブから txt/json で保存できます。
- ガタライズスクリプトは特定の単語を別の単語に置き換えて出力する機能です
- ベーシックなガタライズスクリプトの単語リストはgscript.jsonに定義されています。
- 必要に応じてオリジナルの単語リストを作成し利用することも可能です。

## Lisence

This project is licensed under the MIT License, see the LICENSE.txt file for details
