cmake_minimum_required(VERSION 3.22)
project(wdndds_idl LANGUAGES C CXX)

find_package(fastdds REQUIRED)

add_library(wdndds_idl SHARED
    computePubSubTypes.cxx
    computeTypeObjectSupport.cxx
)
target_include_directories(wdndds_idl PUBLIC ${CMAKE_CURRENT_SOURCE_DIR})
target_link_libraries(wdndds_idl PUBLIC fastdds fastcdr)
set_target_properties(wdndds_idl PROPERTIES
    CXX_STANDARD 17
    CXX_STANDARD_REQUIRED ON
)
target_compile_definitions(wdndds_idl PRIVATE COMPUTE_SOURCE)
