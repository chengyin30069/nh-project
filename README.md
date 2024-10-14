This is a simple bash script to download galleries from nhentai, \
just type `./nh2.sh $NUMBER` and you'll get your gallery at ~/nh/$NUMBER.\
Note that this script will not `mkdir ~/nh` for you, so you'll have to do it yourself or it wouldn't work

There is a known issue with downloading same image for multiple times, \
`find ~/nh -type f -name "*.[1|2|3|4|5|6|7|8|9]*" -delete` after the download works,\
will try to find a work around in the future.

Also check [Kevin's fork](https://github.com/XiaoPanPanKevinPan/nh-project), he rewrote it with different method,\
you can also get it from branch [Kevin's-method](https://github.com/chengyin30069/nh-project/tree/Kevin's-Method)
will be working with him on adding new features on this branch.\
