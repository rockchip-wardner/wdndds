cmake_minimum_required(VERSION 3.22)
project(wdndds_cshim LANGUAGES C CXX)

find_package(fastdds REQUIRED)
list(APPEND CMAKE_PREFIX_PATH "${CMAKE_INSTALL_PREFIX}")
list(APPEND CMAKE_LIBRARY_PATH "${CMAKE_INSTALL_PREFIX}/lib")

add_library(wdndds_cshim SHARED
    wdndds_cshim.cpp
)
target_include_directories(wdndds_cshim PUBLIC
    ${CMAKE_CURRENT_SOURCE_DIR}/../include
    ${CMAKE_CURRENT_SOURCE_DIR}/../idl
    ${CMAKE_INSTALL_PREFIX}/include/wdndds/idl
)
target_link_directories(wdndds_cshim PUBLIC ${CMAKE_INSTALL_PREFIX}/lib)
target_link_libraries(wdndds_cshim PUBLIC wdndds_idl fastdds fastcdr)
set_target_properties(wdndds_cshim PROPERTIES
    CXX_STANDARD 17
    CXX_STANDARD_REQUIRED ON
)