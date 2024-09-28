#!/bin/bash

# help info
if [ "$1" == "help" ] ; then
	echo This is a script used to download and nhentai as pictures
	echo
	echo "(./)nh2.sh [NUMBER|help]"
	echo
	echo NUMBER is the magic number of the gallery
	echo or you may as well use help to print out this help messege
	exit 0
fi

# directory for saving the image
cd ~/nh
mkdir $1
cd $1

# fetch the cover page and save it
declare COVER_HTML="$(curl -sS https://nhentai.net/g/485643/)"
echo "$COVER_HTML" > cover_page.html

# a command to download with auto-retrying
download-with-auto-retry() {
	declare FILENAME=$1
	declare URL=$2
	touch $FILENAME

	curl -sS -o "$FILENAME" "$URL"
	declare LAST_CURL_DOWNLOAD_RET=$?
	declare MAX_RETRY=3
	for i in $(seq 1 "$MAX_RETRY"); do 
		# retry if curl didn't run successfully
		if [ "$LAST_CURL_DOWNLOAD_RET" -eq 0 ]; then
			break;
		fi
		echo "$FILENAME some errors met while downloading. Retrying ($i/$MAX_RETRY)..."
		curl -sS -o "$FILENAME" "$URL"
	done

	# tell the user that some file is downloaded
	if [ "$LAST_CURL_DOWNLOAD_RET" -eq 0 ]; then
		echo "$FILENAME downloaded"
	else
		echo "$FILENAME failed to download"
	fi
}

# extract a list of images that we need to download
# make enter after each html tag 
# -> grep the urls 
#	 - pattern: cover.jpg/png/gif, and t${number}.jpg/png/gif for thumbnails
# -> convert thumbnail filenames to normal files
# -> uniquify the links with awk
#    - notice: there may still be multiple urls for book covers
declare IMAGE_URLS="$(echo "$COVER_HTML" \
	| sed -E 's/>/\n/g' \
	| grep -oEe 'https://t[0-9]+.nhentai\.net/galleries/[0-9]+/([0-9]+t|cover)\.[a-zA-Z]+' \
	| sed -E 's/t(\.[a-zA-Z]{1,10})$/\1/g' \
	| awk '!a[$0]++'
)"

# download each image
declare JOBS=""
for URL in $IMAGE_URLS; do 
	# extract filename 
	declare FILENAME="$(echo "$URL" | sed -E 's/.*\/([^\/]+)/\1/' )"

	# check if file exists; if do, skip it
	if [ -e "$FILENAME" ]; then
		continue
	fi

	# download the file with auto-retrying
	download-with-auto-retry $FILENAME $URL &
	JOBS="$JOBS $!"
done;

# do not exit the program before all the jobs done
for PID in $JOBS; do 
	wait "$PID";
done;

