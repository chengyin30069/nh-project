
# Nhentai Frontend Analysis
By analyze the code of Nhentai's webpage, we can simulate its behaviour and ultimately create a robust way to download the files with the least amount of web requests.

## The URL Rule
### Forming a usable URL
By using developer tools on a page of a book on nhentai, in the debugger tab, we search for `url = ` and find this function (prettyprinted):
```js
e.url = function () {
	var t,
	e;
	return 'page' === this.type ? (t = 'i', e = this.number.toString()) : 'thumbnail' === this.type ? (t = 't', e = ''.concat(this.number, 't')) : (t = 't', e = ''.concat(this.type)),
	'https://'.concat(t, '.nhentai.net/galleries/').concat(this.gallery.media_id, '/').concat(e, '.').concat(this.extension)
}
```
This can be (somehow) traslated back to: 
```js
e.url = function() {
	var t, e;
	if('page' == this.type) {
		t = 'i';
		e = `${this.number}`;
	} else if('thumbnail' === this.type) {
		t = 't';
		e = `${this.number}t`;
	} else {
		t = 't';
		e = `${this.type}`;
	}
	return `https://${t}.nhentai.net/galleries/${this.gallery.media_id}/${e}.${this.extension}`
}
```
So, for thumbnails, it uses `t` server and get `t${PAGE_NUMBER}.jpg/png/gif` (or so). For the original image that shows on pages, it uses `i` server and get `${PAGE_NUMBER}.jpg/png/gif`. However, this is not the final url that we mostly uses.

### Forming a CDN URL
By searching for `media_server`, we can find such a code (prettyprinted):
```js
return e.get_cdn_url = function (t) {
	var e = this.options.media_server;
	return t.replace(
		/\/\/([it])\d*\./,
		(function (t, n) {
			return '//'.concat(n).concat(e, '.')
		})
	)
}
```
This code does so: extract the ``//t.` or `//i.` part from the url we constructed above, and add a number indicating the media server after the i/t.

Also, by searching for `media_server`, we can also find the number in the HTML code (prettyprinted):
```html 
<script>
	window._n_app = new App({
		csrf_token: "...",
		user: {},
		blacklisted_tags: null,
		media_server: 3,
		ads: {
			show_popunders: true
		}
	});
</script>
```
Worth noticing, the `media_server` property isn't necessarily the same among the cover page `/g/${id}/` and the book pages `/g/${id}/${page_number}`. Sometimes, 404 error also comes since some image is not cached on each server. (e.g., 2024/09/29, `https://i5.nhentai.net/galleries/3050812/18.jpg` is 404, but i3 and i7 has images.) To solve this, we should try another server while downloading failed.

## Fetch The Book Info
In another `<script>` tag in the HTML code, we can find something like this: 
```
window._gallery = JSON.parse("...");
```
The `...` part is a JSON string with most character encoded in backslash escape sequence. We can use jq to prettyprint the code. Then, we get something like this: 
```
{
  "id": 529160,
  "media_id": "3050812",
  "title": {
    "english": "[Jido - hanbai-ki (YAMANEKO)] Reguugachi (Made in Abyss)",
    "japanese": "[Jido-販売機 (YAMANEKO)] レグウガチ (メイドインアビス)",
    "pretty": "Reguugachi"
  },
  "images": {
    "pages": [
      {
        "t": "j",
        "w": 1280,
        "h": 1870
      }, 
      ...
    ],
    "cover": {
      "t": "j",
      "w": 350,
      "h": 512
    },
    "thumbnail": {
      "t": "j",
      "w": 250,
      "h": 365
    }
  },
  "scanlator": "",
  "upload_date": 1725768620,
  "tags": [
    {
      "id": 6346,
      "type": "language",
      "name": "japanese",
      "url": "/language/japanese/",
      "count": 274762
    },
    ...
  ],
  "num_pages": 24,
  "num_favorites": 0
}
```
