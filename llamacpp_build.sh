git clone https://github.com/ggerganov/llama.cpp.git llamacpp_ubuntu
cd llamacpp_ubuntu
mkdir build_ubuntu
sudo apt install ninja-build cmake build-essential
cmake -B build_ubuntu -G Ninja -DCMAKE_BUILD_TYPE=Release -DGGML_CUDA=ON
cd build_ubuntu
ninja
cp bin/llama-server ../..