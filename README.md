# About
This is a simple bash script to download galleries from nhentai, \
all the downloaded galleries will be stored at ~/nh/{the six digits code} folder, \
Note that this script will not `mkdir ~/nh` for you, so you'll have to do it yourself or it'll be stored at your current directory \

# Why do I need this instead of their BitTorrent Download?
We all knew, P2P is slow and sucked at security, You can get better download speed using our script since all it does is \
1. fetch the gallery html
2. grep the image links
3. parallel download them via https requests 
thus provides a faster and safer download experience, all you need to do is provide your cookie and \
browser agent version in the script

# Dependencies
* aria2
* wget
* bash

# Using Docker
For Windows users or who just want to use docker, simply build with Dockerfile we provided 
1. `docker build -t nh-project .`
2. `docker run --rm -v "${HOME}/nh:/root/nh" nh-project` (run `bash download.sh nhentai.txt` in docker)
3. (optional) `docker run --rm -it -v "${HOME}/nh:/root/nh" nh-project bash` (run interactively with our scripts)
