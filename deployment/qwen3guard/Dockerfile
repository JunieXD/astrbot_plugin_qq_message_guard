# Reuse the already validated llama.cpp b11561 CPU runtime on this machine.
# The OneJev Python API is not started: the entrypoint is bare llama-server.
ARG LLAMA_IMAGE=onejev-local:cpu
FROM ${LLAMA_IMAGE}
ENTRYPOINT ["/app/llama-server"]
