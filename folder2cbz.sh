#!/bin/bash

find ~/nh -type f -name "*.[0-9]*" -delete
cd ~/nh
ls -tr | grep -E "^[0-9]*$" > tmp.txt
while read -r line
do
	zip -rm "$line".zip "$line"  #moving the entire folder into a zip, meaning that there will only be $line.zip left
	mv "$line".zip "$line".cbz   #changing file format
done < tmp.txt
rm tmp.txt
