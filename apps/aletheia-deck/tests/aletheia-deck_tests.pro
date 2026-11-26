QT += core gui qml quick quickcontrols2 testlib
CONFIG += c++17 testcase
TARGET = aletheia-deck_tests
TEMPLATE = app

INCLUDEPATH += ../src

HEADERS += ../src/backend.h ../src/theme.h ../src/translator.h
SOURCES += aletheia-deck_tests.cpp ../src/backend.cpp ../src/theme.cpp ../src/translator.cpp
RESOURCES += ../src/resources.qrc
