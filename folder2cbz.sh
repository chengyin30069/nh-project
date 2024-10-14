#!/bin/bash

find ~/nh -type f -name "*.[0-9]*" -delete
cd ~/nh || exit 1
ls | grep -E "^[0-9]*$" > tmp.txt
while read -r line
do
	zip -rm "$line".zip "$line"  #zipping the folder, only $line.zip remains
	mv "$line".zip "$line".cbz   #changing file format
done < tmp.txt
rm tmp.txt
