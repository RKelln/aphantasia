# coding: UTF-8
import os
import argparse
import math
import numpy as np
import cv2
import shutil
from imageio import imsave
from googletrans import Translator, constants

import torch
import torchvision
import torch.nn.functional as F

import clip
os.environ['KMP_DUPLICATE_LIB_OK']='True'

from clip_fft import to_valid_rgb, fft_image, slice_imgs, checkout, cvshow, txt_clean
from utils import pad_up_to, basename, file_list, img_list, img_read
try: # progress bar for notebooks 
    get_ipython().__class__.__name__
    from progress_bar import ProgressIPy as ProgressBar
except: # normal console
    from progress_bar import ProgressBar

clip_models = ['ViT-B/32', 'RN50', 'RN50x4', 'RN101']

def get_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('-i',  '--in_txt',  default=None, help='Text file to process')
    parser.add_argument(       '--out_dir', default='_out')
    parser.add_argument('-s',  '--size',    default='1280-720', help='Output resolution')
    parser.add_argument('-r',  '--resume',  default=None, help='Path to saved FFT snapshots, to resume from')
    parser.add_argument('-l',  '--length',  default=180, type=int, help='Total length in sec')
    parser.add_argument(       '--fstep',   default=1, type=int, help='Saving step')
    parser.add_argument('-tr', '--translate', action='store_true')
    parser.add_argument('-t0', '--in_txt0', default=None, help='input text to subtract')
    parser.add_argument(       '--save_pt', action='store_true', help='Save FFT snapshots for further use')
    parser.add_argument(       '--fps',     default=25, type=int)
    parser.add_argument('-v',  '--verbose', default=True, type=bool)
    # training
    parser.add_argument('-m',  '--model',   default='ViT-B/32', choices=clip_models, help='Select CLIP model to use')
    parser.add_argument(       '--steps',   default=200, type=int, help='Total iterations')
    parser.add_argument(       '--samples', default=200, type=int, help='Samples to evaluate')
    parser.add_argument('-lr', '--lrate',   default=0.05, type=float, help='Learning rate')
    # tweaks
    parser.add_argument('-o',  '--overscan', action='store_true', help='Extra padding to add seamless tiling')
    parser.add_argument(       '--keep',    default=None, help='Accumulate imagery: None (random init), last, all')
    parser.add_argument(       '--contrast', default=1., type=float)
    parser.add_argument('-n',  '--noise',   default=0.02, type=float, help='Add noise to suppress accumulation')
    a = parser.parse_args()

    if a.size is not None: a.size = [int(s) for s in a.size.split('-')][::-1]
    if len(a.size)==1: a.size = a.size * 2
    a.modsize = 288 if a.model == 'RN50x4' else 224
    return a

def ema(base, next, step):
    scale_ma = 1. / (step + 1)
    return next * scale_ma + base * (1.- scale_ma)

def main():
    a = get_args()

    # Load CLIP models
    model_clip, _ = clip.load(a.model)
    if a.verbose is True: print(' using model', a.model)
    xmem = {'RN50':0.5, 'RN50x4':0.16, 'RN101':0.33}
    if 'RN' in a.model:
        a.samples = int(a.samples * xmem[a.model])
    workdir = os.path.join(a.out_dir, basename(a.in_txt))
    workdir += '-%s' % a.model if 'RN' in a.model.upper() else ''

    norm_in = torchvision.transforms.Normalize((0.48145466, 0.4578275, 0.40821073), (0.26862954, 0.26130258, 0.27577711))

    if a.in_txt0 is not None:
        if a.verbose is True: print(' subtract text:', basename(a.in_txt0))
        if a.translate:
            translator = Translator()
            a.in_txt0 = translator.translate(a.in_txt0, dest='en').text
            if a.verbose is True: print(' translated to:', a.in_txt0) 
        tx0 = clip.tokenize(a.in_txt0).cuda()
        txt_enc0 = model_clip.encode_text(tx0).detach().clone()

    def process(txt, num):

        global params_start
        if num==0: # initial step
            params, image_f = fft_image([1, 3, *a.size], resume=a.resume)
            params_start = params[0].detach().clone()
            torch.save(params_start, 'tmp.pt') # random init
        else: # further steps
            params, image_f = fft_image([1, 3, *a.size], resume='tmp.pt')
        image_f = to_valid_rgb(image_f)
        optimizer = torch.optim.Adam(params, a.lrate)
    
        if a.verbose is True: print(' ref text: ', txt)
        if a.translate:
            translator = Translator()
            txt = translator.translate(txt, dest='en').text
            if a.verbose is True: print(' translated to:', txt)
        tx = clip.tokenize(txt).cuda()
        txt_enc = model_clip.encode_text(tx).detach().clone()

        out_name = '%02d-%s' % (num, txt_clean(txt))
        out_name += '-%s' % a.model if 'RN' in a.model.upper() else ''
        tempdir = os.path.join(workdir, out_name)
        os.makedirs(tempdir, exist_ok=True)
        
        pbar = ProgressBar(a.steps // a.fstep)
        for i in range(a.steps):
            loss = 0

            noise = a.noise * torch.randn(1, 1, *params[0].shape[2:4], 1).cuda() if a.noise > 0 else None
            img_out = image_f(noise)
            
            imgs_sliced = slice_imgs([img_out], a.samples, a.modsize, norm_in, a.overscan, micro=None)
            out_enc = model_clip.encode_image(imgs_sliced[-1])
            loss -= torch.cosine_similarity(txt_enc, out_enc, dim=-1).mean()
            if a.in_txt0 is not None: # subtract text
                loss += torch.cosine_similarity(txt_enc0, out_enc, dim=-1).mean()
            del img_out, imgs_sliced, out_enc; torch.cuda.empty_cache()

            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

            if i % a.fstep == 0:
                with torch.no_grad():
                    img = image_f(contrast=a.contrast).cpu().numpy()[0]
                checkout(img, os.path.join(tempdir, '%04d.jpg' % (i // a.fstep)), verbose=a.verbose)
                pbar.upd()
                del img

        if a.keep == 'all':
            params_start = ema(params_start, params[0].detach(), num+1)
            torch.save(params_start, 'tmp.pt')
        elif a.keep == 'last':
            torch.save((params_start + params[0].detach()) / 2, 'tmp.pt')
        
        torch.save(params[0], '%s.pt' % os.path.join(workdir, out_name))
        shutil.copy(img_list(tempdir)[-1], os.path.join(workdir, '%s-%d.jpg' % (out_name, a.steps)))
        os.system('ffmpeg -v warning -y -i %s\%%04d.jpg "%s.mp4"' % (tempdir, os.path.join(workdir, out_name)))

    with open(a.in_txt, 'r', encoding="utf-8") as f:
        texts = f.readlines()
        texts = [tt.strip() for tt in texts if len(tt.strip()) > 0 and tt[0] != '#']
    if a.verbose is True: 
        print(' total lines:', len(texts))
        print(' samples:', a.samples)

    for i, txt in enumerate(texts):
        process(txt, i)

    vsteps = int(a.length * 25 / len(texts)) # 25 fps
    tempdir = os.path.join(workdir, '_final')
    os.makedirs(tempdir, exist_ok=True)
    
    def read_pt(file):
        return torch.load(file).cuda()

    if a.verbose is True: print(' rendering complete piece')
    ptfiles = file_list(workdir, 'pt')
    pbar = ProgressBar(vsteps * len(ptfiles))
    for px in range(len(ptfiles)):
        params1 = read_pt(ptfiles[px])
        params2 = read_pt(ptfiles[(px+1) % len(ptfiles)])

        params, image_f = fft_image([1, 3, *a.size], resume=params1)
        image_f = to_valid_rgb(image_f)

        for i in range(vsteps):
            with torch.no_grad():
                img = image_f((params2 - params1) * math.sin(1.5708 * i/vsteps)**2)[0].permute(1,2,0)
                img = torch.clip(img*255, 0, 255).cpu().numpy().astype(np.uint8)
            imsave(os.path.join(tempdir, '%05d.jpg' % (px * vsteps + i)), img)
            if a.verbose is True: cvshow(img)
            pbar.upd()

    os.system('ffmpeg -v warning -y -i %s\%%05d.jpg "%s.mp4"' % (tempdir, os.path.join(a.out_dir, basename(a.in_txt))))
    if a.keep is True: os.remove('tmp.pt')


if __name__ == '__main__':
    main()
