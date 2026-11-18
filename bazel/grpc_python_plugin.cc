// Match grpcio-tools 1.71.0's GeneratorConfiguration, using the BCR source library.
#include "src/compiler/config.h"
#include "src/compiler/protobuf_plugin.h"
#include "src/compiler/python_generator.h"

int main(int argc, char* argv[]) {
  grpc_python_generator::GeneratorConfiguration config("1.71.0");
  grpc_python_generator::PythonGrpcGenerator generator(config);
  return grpc::protobuf::compiler::PluginMain(argc, argv, &generator);
}
