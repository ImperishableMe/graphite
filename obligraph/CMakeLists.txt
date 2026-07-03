cmake_minimum_required(VERSION 3.14)
project(obliDB VERSION 0.1.0 LANGUAGES C CXX)

# Require C++20
set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

# Enable AVX2 support
add_compile_definitions(USE_AVX2)

# add compile definitions for debugging
add_compile_definitions(OBL_DEBUG)

# For profiling/debug, disable optimization. For release build, use -O3
set(CMAKE_C_FLAGS_DEBUG "${CMAKE_C_FLAGS_DEBUG} -O0")
set(CMAKE_CXX_FLAGS_DEBUG "${CMAKE_CXX_FLAGS_DEBUG} -O0")

set(CMAKE_CXX_FLAGS "${CMAKE_CXX_FLAGS} -pthread -march=native")
set(CMAKE_CXX_FLAGS_RELEASE "${CMAKE_CXX_FLAGS_RELEASE} -O3")

# Find Python3 (needed for data generation script)
find_package(Python3 REQUIRED)

# Add oblivious_hashmap as a subdirectory
add_subdirectory(${CMAKE_CURRENT_SOURCE_DIR}/../oblivious_hashmap ${CMAKE_BINARY_DIR}/oblivious_hashmap)

# Set up FetchContent for dependencies
include(FetchContent)

# Google Test
FetchContent_Declare(
  googletest
  URL https://github.com/google/googletest/archive/refs/tags/v1.14.0.zip
)
# For Windows: Prevent overriding the parent project's compiler/linker settings
set(gtest_force_shared_crt ON CACHE BOOL "" FORCE)


# Make dependencies available
FetchContent_MakeAvailable(googletest)


# Create directory structure if not exists
file(MAKE_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}/include)
file(MAKE_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}/src)
file(MAKE_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}/tests)
file(MAKE_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}/scripts)

add_custom_target(generate_test_data
    COMMAND ${Python3_EXECUTABLE} ${GENERATE_SCRIPT}
    BYPRODUCTS ${TEST_DATA_FILES}
    COMMENT "Generating test data files"
    VERBATIM
)

# include core and definitions
include_directories(${CMAKE_CURRENT_SOURCE_DIR}/include)

add_library(core
    src/definitions.cpp
    src/operators.cpp
    src/oneHop.cpp
    src/par_obl_primitives.cpp
    src/config.cpp
    src/schema_parser.cpp
)

target_compile_definitions(core PRIVATE XXH_INLINE_ALL)

target_include_directories(core PUBLIC
    ${CMAKE_CURRENT_SOURCE_DIR}/include
    ${CMAKE_CURRENT_SOURCE_DIR}/../oblivious_hashmap/include
)

target_link_libraries(core PUBLIC ObliviousHashmapLib)

# Driver executable
add_executable(driver src/driver.cpp)
target_link_libraries(driver PRIVATE core)

# ldbc queries executable
add_executable(ldbc_queries src/ldbc_is_3.cpp)
target_link_libraries(ldbc_queries PRIVATE core)

# custom dataset queries executable
add_executable(custom_dataset_queries src/custom_dataset_query.cpp)
target_link_libraries(custom_dataset_queries PRIVATE core)

# Banking one-hop executable (for query decomposition)
add_executable(banking_onehop src/banking_onehop.cpp)
target_link_libraries(banking_onehop PRIVATE core)

# IBM AML-Data one-hop executable (W4, mirror of banking_onehop with AML schema)
add_executable(ibm_aml_onehop src/ibm_aml_onehop.cpp)
target_link_libraries(ibm_aml_onehop PRIVATE core)

# SNAP cit-Patents one-hop executable (W3, mirror of banking_onehop with patent schema)
add_executable(snap_patents_onehop src/snap_patents_onehop.cpp)
target_link_libraries(snap_patents_onehop PRIVATE core)

# Enable testing
enable_testing()

# Unit tests executable
add_executable(unit_tests
    tests/catalogTests.cpp
    tests/operatorTests.cpp
    tests/oneHopTests.cpp
    tests/parallelSortTests.cpp
    tests/obliviousPrimitivesTests.cpp
    tests/cypherSchemaTests.cpp
)
target_link_libraries(unit_tests 
    PRIVATE core
    PRIVATE gtest_main
    PRIVATE gmock_main
)

# Ensure test data is generated before running tests
# add_dependencies(unit_tests generate_test_data)

# Register test with CTest
add_test(NAME unit_tests COMMAND unit_tests)

# # Python server roundtrip test
# add_test(
#     NAME server_roundtrip
#     COMMAND ${CMAKE_CURRENT_SOURCE_DIR}/tests/run_server_test.sh ${CMAKE_BINARY_DIR}
# )

# # Make the test depend on test data generation
# set_tests_properties(server_roundtrip PROPERTIES DEPENDS generate_test_data)
