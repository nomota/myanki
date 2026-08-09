#!/bin/sh

sh ./stop.sh 
umask 0
python3 myanki.py # >./out 2>./err &
# sh background.sh > ./background.out 2> ./background.err &
ps -ef | grep myanki.py | grep -v grep 
# ps -ef | grep background.sh | grep -v grep


