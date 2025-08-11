# About
This is a simple bash script to download galleries from nhentai, \
all the downloaded galleries will be stored at ~/nh/{the six digits code} folder

# Why do I need this instead of their BitTorrent Download?
We all knew, P2P is slow and vulnerable to Man-in-the-middle attack, \
You can get better download speed using our script since all it does is 
1. fetch the gallery html file
2. grep the image links
3. parallel download them via https requests

thus provides a faster and safer download experience, all you need to do is provide your cookie and \
browser agent version in the script

# Dependencies
* aria2
* wget
* bash
* procps (for alpine linux)

# Using Docker
For Windows users or who just want to use docker, simply build with Dockerfile we provided 
1. `docker build -t nh-project .`
2. `docker run --rm -v "${HOME}/nh:/root/nh" nh-project` (run `bash download.sh nhentai.txt` in docker)
3. (optional) `docker run --rm -it -v "${HOME}/nh:/root/nh" nh-project bash` (run interactively with our scripts)

## Disclaimer / 聲明

### English

This project is intended **solely for educational and research purposes**.  
We do not encourage, promote, or endorse any activity that violates copyright laws, licensing terms, or applicable regulations, including but not limited to piracy or unauthorized distribution of copyrighted material.  

By using this project, you agree to the following conditions:  
1. **Compliance with Laws** – You are solely responsible for ensuring that your use of this project complies with all laws and regulations in your jurisdiction.  
2. **Temporary Storage** – Any files obtained through this project must be **permanently deleted within 24 hours** of download.  
3. **Prohibition of Commercial Use** – Downloaded content must **not** be used for illegal, commercial, or profit-generating purposes.  
4. **No Liability** – The contributors of this project assume **no liability** for any misuse, damage, or legal consequences resulting from the use of this project.  

By continuing to use this project, you acknowledge and agree to the above terms in full.  

### 中文

本專案**僅供教育與研究用途**。  
我們不鼓勵、宣傳或支持任何違反著作權法、授權條款或相關法規以及侵犯他人智慧財產權之任何法律行為，包括但不限於盜版、未經授權散佈受著作權法保護之內容。 

使用本專案即表示您同意以下條款：  
1. **遵守法律** – 您必須自行確保使用本專案的行為符合您所在司法管轄區的所有法律與規定。  
2. **臨時儲存** – 透過本專案下載的任何檔案，必須在下載後 **24 小時內永久刪除**。  
3. **禁止商業用途** – 所下載的內容**不得**用於任何非法、商業或營利目的。  
4. **免責聲明** – 本專案貢獻者對於任何因使用本專案而導致的濫用、損害或法律後果，**不承擔任何責任**。  

繼續使用本專案即表示您已完整理解並同意上述所有條款。
