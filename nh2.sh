#!/bin/bash

if [ "$1" == "help" ] ; then
	echo This is a script used to download and nhentai as pictures
	echo
	echo "(./)nh2.sh [NUMBER|help]"
	echo
	echo NUMBER is the magic number of the gallery
	echo or you may as well use help to print out this help messege
	exit 0
fi

cd
mkdir $1
cd $1

for i in $(seq 1 500)
do
#download html of nhentai.net/g/NUMBER/$i
	curl -s https://nhentai.net/g/$1/$i/ > tmp$i.html
#check if it's 404 or not, break if it is
	# echo $(grep -o -e "404 - Not Found" tmp.html)
	if [ "$(grep -o -e "404 - Not Found" tmp$i.html)" == "404 - Not Found" ]; then
		rm tmp$i.html
		break
	fi
	# echo $i
#grep to get the source of image
	img=$(grep -o -e "https://i[1|2|3|4|5|6|7|8|9].nhentai.net/galleries/[0|1|2|3|4|5|6|7|8|9]*/[1|2|3|4|5|6|7|8|9][0|1|2|3|4|5|6|7|8|9]*.[j|p][p|n]g" tmp$i.html)
	# echo $img
#curl to download it
	curl -O -s $img &
	rm tmp$i.html
done

# rm tmp.html
