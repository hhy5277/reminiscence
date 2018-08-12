import os
import json
import shutil
import logging
from functools import partial
from urllib.parse import urlparse
from mimetypes import guess_extension, guess_type
from datetime import datetime
from django.utils import timezone
from django.conf import settings
from vinanti import Vinanti
from bs4 import BeautifulSoup
from .models import Library, Tags, URLTags, UserSettings
from .summarize import Summarizer

import subprocess
from celery.decorators import task

logger = logging.getLogger(__name__)


class DBAccess:
    
    vnt = Vinanti(block=False, hdrs={'User-Agent':settings.USER_AGENT},
                  max_requests=settings.VINANTI_MAX_REQUESTS,
                  backend=settings.VINANTI_BACKEND, timeout=300)
    vnt_task = Vinanti(block=False, group_task=False,
                       backend='function',
                       multiprocess=settings.MULTIPROCESS_VINANTI,
                       max_requests=settings.MULTIPROCESS_VINANTI_MAX_REQUESTS)
    
    @classmethod
    def add_new_url(cls, usr, request, directory, row):
        url_name = request.POST.get('add_url', '')
        if url_name:
            if url_name.startswith('ar:'):
                url_name = url_name[3:].strip()
                archive_html = True
            else:
                archive_html = False
            if row:
                settings_row = row[0]
            else:
                settings_row = None
            url_list = Library.objects.filter(usr=usr,
                                              directory=directory,
                                              url=url_name)
            if not url_list and url_name:
                cls.process_add_url(usr, url_name,
                                    directory, archive_html, 
                                    settings_row=settings_row)
                                
    @classmethod
    def process_add_url(cls, usr, url_name, directory,
                        archive_html, row=None,
                        settings_row=None, media_path=None):
        part = partial(cls.url_fetch_completed, usr, url_name,
                       directory, archive_html, row, settings_row,
                       media_path)
        cls.vnt.get(url_name, onfinished=part)
    
    @classmethod
    def url_fetch_completed(cls, usr, url_name, directory,
                            archive_html, row, settings_row,
                            media_path, *args):
        ext = None
        save = False
        save_text = False
        favicon_link = None
        summary = 'none'
        req = args[-1]
        tags_list = []
        save_summary = False
        if req and req.content_type:
            if ';' in req.content_type:
                content_type = req.content_type.split(';')[0].strip()
            else:
                content_type = req.content_type
            if content_type == 'text/plain':
                ext = '.txt'
            else:
                ext = guess_extension(content_type)
            print(content_type, '------', ext)
        if req and req.html and not req.binary:
            if 'text/html' in req.content_type:
                soup = BeautifulSoup(req.html, 'html.parser')
                if soup.title:
                    title = soup.title.text
                else:
                    title = url_name.rsplit('/')[-1]
                ilink = soup.find('link', {'rel':'icon'})
                slink = soup.find('link', {'rel':'shortcut icon'})
                if ilink:
                    favicon_link = cls.format_link(ilink.get('href'), url_name)
                elif slink:
                    favicon_link = cls.format_link(slink.get('href'), url_name)
                else:
                    for link in soup.find_all('link'):
                        rel = link.get('href')
                        if (rel and (rel.endswith('.ico') or '.ico' in rel)):
                            favicon_link = cls.format_link(rel, url_name)
                    if not favicon_link:
                        urlp = urlparse(url_name)
                        favicon_link = urlp.scheme + '://' + urlp.netloc + '/favicon.ico'
                        
                if archive_html or (settings_row and settings_row.auto_archive):
                    save_text = True
                if settings_row and (settings_row.autotag or settings_row.auto_summary):
                    summary, tags_list = Summarizer.get_summary_and_tags(req.html,
                                                                         settings_row.total_tags)
            else:
                title = url_name.rsplit('/')[-1]
                save = True
        elif req and req.binary:
            title = url_name.rsplit('/')[-1]
            save = True
        else:
            ext = '.bin'
            title = url_name.rsplit('/', 1)[-1]
        if row is None:
            row = Library.objects.create(usr=usr,
                                         directory=directory,
                                         url=url_name, title=title,
                                         summary=summary,
                                         timestamp=timezone.now())
        else:
            print('row - exists')
        if not media_path:
            if ext and ext.startswith('.'):
                out_dir = ext[1:].upper()
            else:
                out_dir = str(ext).upper()
            if not ext:
                print(req.content_type)
            out_title = str(row.id) + str(ext)
            media_dir = os.path.join(settings.ARCHIVE_LOCATION, out_dir)
            if not os.path.exists(media_dir):
                os.makedirs(media_dir)
            if not os.path.exists(settings.FAVICONS_STATIC):
                os.makedirs(settings.FAVICONS_STATIC)
            media_path_parent = os.path.join(media_dir, str(row.id))
            final_favicon_path = os.path.join(settings.FAVICONS_STATIC, str(row.id) + '.ico')
            media_path = os.path.join(media_path_parent, out_title)
            row.media_path = media_path
            row.save()
            if not os.path.exists(final_favicon_path) and favicon_link:
                cls.vnt.get(favicon_link, out=final_favicon_path)
        elif media_path and row:
            final_favicon_path = os.path.join(settings.FAVICONS_STATIC, str(row.id) + '.ico')
            media_path_parent, out_title = os.path.split(media_path)
            if settings_row and settings_row.auto_summary and summary:
                row.summary = summary
            if settings_row and not tags_list:
                row.save()
            else:
                save_summary = True
            if not os.path.exists(final_favicon_path) and favicon_link:
                cls.vnt.get(favicon_link, out=final_favicon_path)
        #print(favicon_link, final_favicon_path)
        if save or save_text:
            if not os.path.exists(media_path_parent):
                os.makedirs(media_path_parent)
            if save:
                #req.save(req.request_object, media_path)
                cls.vnt.get(url_name, out=media_path)
            else:
                with open(media_path, 'w') as fd:
                    fd.write(req.html)
            if settings_row and ext in ['.htm', '.html']:
                cls.convert_html_pdf(media_path_parent, settings_row,
                                     row, url_name, media_path)
        if settings_row and tags_list:
            if save_summary:
                cls.edit_tags(usr, row.id, ','.join(tags_list), '', old_row=row)
            else:
                cls.edit_tags(usr, row.id, ','.join(tags_list), '')
        return row.id
    
    @classmethod
    def save_in_binary_format(cls, usr, request, directory):
        url_list = []
        for key, value in request.FILES.items():
            title = value.name
            content = value.read()
            ext = None
            content_type = guess_type(title)[0]
            if content_type and content_type == 'text/plain':
                ext = '.txt'
            elif content_type:
                ext = guess_extension(content_type)
            print(content_type, '------', ext)
            if not ext:
                ext = '.bin'
            out_dir = ext[1:].upper()
            row = Library.objects.create(usr=usr,
                                         directory=directory,
                                         title=title, timestamp=timezone.now())
            
            out_title = str(row.id) + str(ext)
            media_dir = os.path.join(settings.ARCHIVE_LOCATION, out_dir)
            if not os.path.exists(media_dir):
                os.makedirs(media_dir)
            
            media_path_parent = os.path.join(media_dir, str(row.id))
            if not os.path.exists(media_path_parent):
                os.makedirs(media_path_parent)
                    
            media_path = os.path.join(media_path_parent, out_title)
            row.media_path = media_path
            url = '/{}/{}/{}/archive'.format(usr.username, directory, row.id)
            row.url = url
            row.save()
            with open(media_path, 'wb') as fd:
                fd.write(content)
            url_list.append(url)
            
        return url_list
    
    @classmethod
    def convert_html_pdf(cls, media_path_parent,
                         settings_row, row, url_name,
                         media_path):
        if settings_row.save_pdf:
            pdf = os.path.join(media_path_parent, str(row.id)+'.pdf')
            cmd = [
                'wkhtmltopdf', '--custom-header',
                'User-Agent', settings.USER_AGENT,
                '--javascript-delay', '500',
                url_name, pdf
            ]
            if settings.USE_XVFB:
                cmd = ['xvfb-run', '--auto-servernum'] + cmd
            if settings.USE_CELERY:
                cls.convert_to_pdf_png.delay(cmd)
            else:
                cls.vnt_task.function(
                    cls.convert_to_pdf_png_task, cmd,
                    onfinished=partial(cls.finished_processing, 'pdf')
                )
        if settings_row.save_png:
            png = os.path.join(media_path_parent, str(row.id)+'.png')
            cmd = [
                'wkhtmltoimage', '--quality', str(settings_row.png_quality),
                '--custom-header', 'User-Agent', settings.USER_AGENT,
                '--javascript-delay', '500',
                url_name, png
            ]
            if settings.USE_XVFB:
                cmd = ['xvfb-run', '--auto-servernum'] + cmd
            if settings.USE_CELERY:
                cls.convert_to_pdf_png.delay(cmd)
            else:
                cls.vnt_task.function(
                    cls.convert_to_pdf_png_task, cmd,
                    onfinished=partial(cls.finished_processing, 'image')
                )
    
    @classmethod
    def finished_processing(cls, val, *args):
        logger.info('{}-->>>>finished--->>>{}'.format(val, args))
        
    def convert_to_pdf_png_task(cmd):
        if os.name == 'posix':
            subprocess.call(cmd)
        else:
            subprocess.call(cmd, shell=True)
        return True
    
    @task(name="convert-to-pdf-png")
    def convert_to_pdf_png(cmd):
        if os.name == 'posix':
            subprocess.call(cmd)
        else:
            subprocess.call(cmd, shell=True)
    
    @staticmethod
    def get_rows_by_directory(usr, directory=None, search=None, search_mode='title'):
        
        usr_list = []
        
        if search and search_mode != 'dir':
            if search_mode == 'title':
                usr_list = Library.objects.filter(usr=usr, title__icontains=search).order_by('-timestamp')
            elif search_mode == 'url':
                usr_list = Library.objects.filter(usr=usr, url__icontains=search).order_by('-timestamp')
            elif search_mode == 'tag':
                usr_list = Library.objects.filter(usr=usr, tags__icontains=search).order_by('-timestamp')
            elif search_mode == 'summary':
                usr_list = Library.objects.filter(usr=usr, summary__icontains=search).order_by('-timestamp')
        else:
            if not directory and search and search_mode == 'dir':
                directory = search
            usr_list = Library.objects.filter(usr=usr, directory=directory).order_by('-timestamp')
                        
        nusr_list = []
        for row in usr_list:
            if row.url:
                if not row.tags:
                    tags = []
                else:
                    tags = row.tags.split(',')
                nusr_list.append(
                    (row.title, row.url, row.id, row.timestamp,
                     tags, row.directory, row.media_path)
                )
        return nusr_list

    @staticmethod
    def get_rows_by_tag(usr, tagname):
        tagobj = Tags.objects.filter(tag=tagname)
        directory = 'tag'
        usr_list = []
        if tagobj:
            usr_list = URLTags.objects.select_related('url_id').filter(usr_id=usr,
                                                                       tag_id=tagobj[0])
            udict = {}
            tag_list = [tagname]
            for i in usr_list:
                uid = i.url_id.url
                dirname = i.url_id.directory
                udict.update(
                    {
                        uid:[
                            i.url_id.title, uid, i.url_id.id,
                            i.url_id.timestamp, [tagname],
                            dirname, i.url_id.media_path
                        ]
                    }
                )
            usr_list = [tuple(value) for key, value in udict.items()]
            return usr_list
        else:
            return None

    @staticmethod
    def populate_usr_list(usr, usr_list, create_dict=False):
        if create_dict:
            nlist = {}
        else:
            nlist = []
        index = 1
        username = usr.username
        for title, url, idd, timestamp, tag, directory, media_path in usr_list:
            base_dir = '/{}/{}/{}'.format(usr, directory, idd)
            base_remove = base_dir + '/remove'
            base_et = base_dir + '/edit-bookmark'
            move_single = base_dir + '/move-bookmark'
            move_multiple = base_dir + '/move-bookmark-multiple'
            base_eu = base_dir + '/edit-url'
            read_url = base_dir + '/read'
            if media_path and os.path.exists(media_path):
                archive_media = base_dir + '/archive'
            else:
                archive_media = url
            netloc = urlparse(url).netloc
            if len(netloc) > 20:
                netloc = netloc[:20]+ '..'
            timestamp = timestamp.strftime("%d %b %Y")
            final_favicon_path = os.path.join(settings.FAVICONS_STATIC, str(idd) + '.ico')
            if os.path.exists(final_favicon_path):
                fav_path = settings.STATIC_URL + 'favicons/{}.ico'.format(idd)
            else:
                fav_path = ""
            if create_dict:
                nlist.update(
                        {
                            index:{
                                'title':title, 'netloc':netloc, 'url':url,
                                'edit-bookmark':base_et, 'remove-url':base_remove,
                                'timestamp': timestamp, 'tag':tag,
                                'move-bookmark':move_single, 
                                'move-multi': move_multiple, 'usr':username,
                                'archive-media':archive_media, 'directory':directory,
                                'read-url':read_url, 'id': idd, 'fav-path': fav_path
                            }
                        }
                    )
            else:
                nlist.append(
                    [
                        index, title, netloc, url, base_et, base_remove,
                        timestamp, tag, move_single, move_multiple,
                        archive_media, directory, read_url, idd, fav_path
                    ]
                )
            index += 1
        return nlist
    
    @staticmethod
    def format_link(lnk, url):
        ourl = urlparse(url)
        ourld = ourl.scheme + '://' + ourl.netloc
        if lnk and lnk != '#':
            if lnk.startswith('//'):
                lnk = ourl.scheme + ':' + lnk
            elif lnk.startswith('/'):
                lnk = ourld + lnk
            elif lnk.startswith('./'): 
                lnk = url.rsplit('/', 1)[0] + lnk[1:]
            elif lnk.startswith('../'):
                lnk = url.rsplit('/', 2)[0] + lnk[2:]
            elif not lnk.startswith('http'):
                lnk = ourld + '/' + lnk
        return lnk
    
    @staticmethod
    def remove_url_link(url_id=None, row=None):
        if row:
            url_id = row.id
        elif url_id:
            qlist = Library.objects.filter(id=url_id)
            if qlist:
                row = qlist[0]
        if row:
            media_path = row.media_path
            if media_path and os.path.exists(media_path):
                base_dir_url, file_name = os.path.split(media_path)
                base_dir_id, dir_id = os.path.split(base_dir_url)
                resource_dir = os.path.join(settings.ARCHIVE_LOCATION, 'resources', str(url_id))
                if dir_id.isnumeric():
                    ndir_id = int(dir_id)
                    if ndir_id == url_id:
                        shutil.rmtree(base_dir_url)
                        logger.info('removing {}'.format(base_dir_url))
                    if os.path.exists(resource_dir):
                        shutil.rmtree(resource_dir)
                        logger.info('removing {}'.format(resource_dir))
            row.delete()

    @staticmethod
    def move_bookmarks(usr, request, url_id=None, single=True):
        msg = 'Nothing Moved'
        if single and url_id:
            move_to_dir = request.POST.get('move_to_dir', '')
            print(url_id, request.POST)
            if move_to_dir:
                Library.objects.filter(id=url_id).update(directory=move_to_dir)
            msg = 'Moved to {}'.format(move_to_dir)
        elif not single:
            move_to_dir = request.POST.get('move_to_dir', '')
            move_links = request.POST.get('move_links', '')
            if move_links:
                move_links_list = [i.strip() for i in move_links.split(',') if i.strip()]
            else:
                move_links_list = []
            if move_to_dir and move_links_list:
                for link in move_links_list:
                    if link.isnumeric():
                        link_id = int(link)
                        Library.objects.filter(id=link_id).update(directory=move_to_dir)
            msg = 'Moved {} links to {}'.format(move_to_dir, len(move_links_list))
        return msg

    @staticmethod
    def edit_bookmarks(usr, request, url_id):
        title = request.POST.get('new_title', '')
        nurl = request.POST.get('new_url', '')
        tags = request.POST.get('new_tags', '')
        tags_old = request.POST.get('old_tags', '')
        print(url_id, request.POST)
        msg = 'Edited'
        if title and nurl:
            Library.objects.filter(id=url_id).update(title=title, url=nurl)
            msg = msg + ' Title and Link'
        elif title:
            Library.objects.filter(id=url_id).update(title=title)
            msg = msg + ' Title'
        elif nurl:
            Library.objects.filter(id=url_id).update(url=nurl)
            msg = msg + ' Link'
        if tags or tags_old:
            msg = DBAccess.edit_tags(usr, url_id, tags, tags_old) 
        return msg
        
    @staticmethod
    def edit_tags(usr, url_id, tags, tags_old, old_row=None):
        tags_list = [i.lower().strip() for i in tags.split(',')]
        tags_list_library = ','.join(list(set(tags_list)))
        tags_list_old = [i.lower().strip() for i in tags_old.split(',')]
        tags_list = [i for i in tags_list if i]
        tags_list_old = [i for i in tags_list_old if i]
        all_tags = Tags.objects.all()
        
        tags_new_add = set(tags_list) - set(tags_list_old)
        tags_old_delete = set(tags_list_old) - set(tags_list)
        insert_list = []
        for tag in tags_list:
            if not all_tags.filter(tag=tag).exists():
                insert_list.append(Tags(tag=tag))
            else:
                logger.info('Tag: {} exists'.format(tag))
        if insert_list:
            Tags.objects.bulk_create(insert_list)
        if old_row:
            lib_obj = old_row
        else:
            lib_list = Library.objects.filter(id=url_id)
            lib_obj = lib_list[0]
        lib_obj.tags = tags_list_library
        lib_obj.save()
        tagins_list = []
        for tag in tags_new_add:
            tag_obj = Tags.objects.filter(tag=tag)
            tagid = URLTags.objects.filter(usr_id=usr,
                                           url_id=lib_obj,
                                           tag_id=tag_obj[0])
            if not tagid:
                row = tagins_list.append(
                        URLTags(
                            usr_id=usr,
                            url_id=lib_obj,
                            tag_id=tag_obj[0]
                        )
                )
        if tagins_list:
            URLTags.objects.bulk_create(tagins_list)
            
        for tag in tags_old_delete:
            tag_obj = Tags.objects.filter(tag=tag)
            tagid = URLTags.objects.filter(usr_id=usr,
                                           url_id=lib_obj,
                                           tag_id=tag_obj[0])
            if tagid:
                URLTags.objects.filter(usr_id=usr,
                                       url_id=lib_obj,
                                       tag_id=tag_obj[0]).delete()
        msg = ('Edited Tags: new-tags-addition={}::old-tags-delete={}'
               .format(tags_new_add, tags_old_delete))
        return msg
