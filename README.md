This is an simple bash script to download galleries from nhentai, \
all the downloaded galleries will be stored at ~/nh/{the six digits code} folder, \
Note that this script will not `mkdir ~/nh` for you, so you'll have to do it yourself or it'll be stored at your current directory

There is a known issue with downloading same image for multiple times, \
`find ~/nh -type f -name "*.[1|2|3|4|5|6|7|8|9]*" -delete` after the download works,\
will try to find a work around in the future.
