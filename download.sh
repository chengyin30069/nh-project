#!/bin/bash
while read -r line
do
	./nh2.sh "$line"
done < "$1"
