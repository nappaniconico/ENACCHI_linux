git clone https://github.com/ggerganov/llama.cpp.git llamacpp_ubuntu
cd llamacpp_ubuntu
mkdir build_ubuntu
sudo apt install ninja-build cmake build-essential
if nvidia-smi >/dev/null 2>&1; then
    echo "GPUあり"
    cmake -B build_ubuntu -G Ninja -DCMAKE_BUILD_TYPE=Release
cd build_ubuntu
else
    echo "GPUなし"
    cmake -B build_ubuntu -G Ninja -DCMAKE_BUILD_TYPE=Release -DGGML_CUDA=ON
cd build_ubuntu
fi
ninja
cp bin/llama-server ../..