#!/bin/sh

ps -ef | grep myanki.py | grep -v grep | awk '{print $2}'  | xargs kill -9 >/dev/null 2>/dev/null
# ps -ef | grep background.sh | grep -v grep | awk '{print $2}'  | xargs kill -9 >/dev/null 2>/dev/null

