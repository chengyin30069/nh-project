#!/bin/bash

echo "Start zipping galleries"
find ~/nh -type f -name "*.[0-9]*" -delete
cd ~/nh
ls -tr | grep -E "^[0-9]*$" > tmp.txt
galleries=$(cat tmp.txt | wc -l)
i=1
while read -r line
do
	time zip -rm -q -9 "$line".zip "$line"  #moving the entire folder into a zip, meaning that there will only be $line.zip left
	mv "$line".zip "$line".cbz   #changing file format
	echo "$i/$galleries done"
	i=$((i+1))
done < tmp.txt
rm tmp.txt
