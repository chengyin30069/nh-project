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

cd ~/nh || exit 1
if [ -e "$1".cbz ]; then
	echo "Already downloaded this gallery!"
	exit 0
fi
mkdir "$1" || echo "Folder already exists"
cd "$1" || exit 1

declare max_page=0

touch tmp.html

curl -s https://nhentai.net/g/"$1"/ > tmp.html

if [ "$(grep -o -E "404 - Not Found" tmp.html)" == "404 - Not Found" ]; then
	echo "Gallery does not exists"
	cd ..
	rm -r "$1"
	exit 1
fi

for i in $(seq 1 500)
do
#skip if image already exists, should be alright
	if [ -e "$i".jpg ]; then
		continue
	fi
	if [ -e "$i".png ]; then
		continue
	fi
	if [ -e "$i".gif ]; then
		continue
	fi

#download html of nhentai.net/g/NUMBER/$i
	curl -s https://nhentai.net/g/"$1"/"$i"/ > tmp.html

#check if it's 404 or not, break if it is
	# echo $(grep -o -e "404 - Not Found" tmp.html)
	if [ "$(grep -o -e "404 - Not Found" tmp.html)" == "404 - Not Found" ]; then
		max_page=$((i-1))
		break
	fi
	# echo $i


#grep to get the source of image
	img=$(grep -o -E "https://i[1-9].nhentai.net/galleries/[0-9]*/[1-9][0-9]*.(jpg|png|gif)" tmp.html)
	# echo $img

#wget to download it
	file_type=${img#*.}
	file_type=${file_type#*.}
	file_type=${file_type#*.}
	if [ ! -f "$i"."$file_type" ]; then
		wget -q "$img" &
	fi
	echo "$1: $i"
done

#echo $max_page

# recheck
for k in $(seq 1 50)
do
	flag=0
	sleep 1 #wait for images to be downloaded
	for j in $(seq 1 10)
	do
		flag=0
		for i in $(seq 1 $max_page)
		do
			#skip if file already exists, ought to be right
			if [ -e "$i".jpg ]; then
				continue
			fi
			if [ -e "$i".png ]; then
				continue
			fi
			if [ -e "$i".gif ]; then
				continue
			fi

			#flaged since there's still image loss
			flag=1

			#resend html request
			curl -s https://nhentai.net/g/"$1"/"$i"/ > tmp.html
			img=$(grep -o -E "https://i[1-9].nhentai.net/galleries/[0-9]*/[1-9][0-9]*.(jpg|png|gif)" tmp.html)

			wget -q "$img" &
			# echo "$i "
		done

		if [ $flag -eq 0 ]; then
			break
		fi
	done
	if [ $flag -eq 0 ]; then
		break
	fi
done

rm tmp.html
# rm tmp*.html
# ls | grep -P "[0|1|2|3|4|5|6|7|8|9]$" | xargs -d"\n" rm
