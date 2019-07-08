'''
Train a NN to model a 3D EM density map
    given 2D projections with angular assignments
'''
import numpy as np
import sys, os
import argparse
import pickle
from datetime import datetime as dt

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import DataLoader

sys.path.insert(0,os.path.abspath(os.path.dirname(__file__))+'/lib-python')
import mrc
import utils
import fft
import dataset
import ctf

from lattice import Lattice
from models import FTPositionalDecoder 

log = utils.log
vlog = utils.vlog

def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('particles', help='Particle stack file (.mrcs)')
    parser.add_argument('rot', help='Rotation matrix for each particle (.pkl)')
    parser.add_argument('--trans', help='Optionally provide translations for each particle (.pkl)')
    parser.add_argument('--tscale', type=float, default=1.0)
    parser.add_argument('--norm', type=float, nargs=2, default=None, help='Data normalization as shift, 1/scale (default: mean, std of dataset)')
    parser.add_argument('--invert-data', action='store_true', help='Invert data sign')
    parser.add_argument('--ctf', metavar='pkl', type=os.path.abspath, help='CTF parameters (.pkl)')
    parser.add_argument('-o', '--outdir', type=os.path.abspath, required=True, help='Output directory to save model')
    parser.add_argument('--load', type=os.path.abspath, help='Initialize training from a checkpoint')
    parser.add_argument('--checkpoint', type=int, default=1, help='Checkpointing interval in N_EPOCHS (default: %(default)s)')
    parser.add_argument('--log-interval', type=int, default=1000, help='Logging interval in N_IMGS (default: %(default)s)')
    parser.add_argument('-v','--verbose',action='store_true',help='Increaes verbosity')
    parser.add_argument('--seed', type=int, default=np.random.randint(0,100000), help='Random seed')
    parser.add_argument('--lazy', action='store_true', help='Use if full dataset is too large to fit in memory')
    parser.add_argument('--l-extent', type=float, default=1.0, help='Coordinate lattice size (default: %(default)s)')

    group = parser.add_argument_group('Training parameters')
    group.add_argument('-n', '--num-epochs', type=int, default=10, help='Number of training epochs (default: %(default)s)')
    group.add_argument('-b','--batch-size', type=int, default=100, help='Minibatch size (default: %(default)s)')
    group.add_argument('--wd', type=float, default=0, help='Weight decay in Adam optimizer (default: %(default)s)')
    group.add_argument('--lr', type=float, default=1e-3, help='Learning rate in Adam optimizer (default: %(default)s)')

    group = parser.add_argument_group('Network Architecture')
    group.add_argument('--layers', type=int, default=10, help='Number of hidden layers (default: %(default)s)')
    group.add_argument('--dim', type=int, default=128, help='Number of nodes in hidden layers (default: %(default)s)')

    return parser

def save_checkpoint(model, lattice, optim, epoch, norm, out_mrc, out_weights):
    model.eval()
    vol = model.eval_volume(lattice.coords, lattice.D, lattice.extent, norm)
    mrc.write(out_mrc, vol.astype(np.float32))
    torch.save({
        'norm': norm,
        'epoch':epoch,
        'model_state_dict':model.state_dict(),
        'optimizer_state_dict':optim.state_dict(),
        }, out_weights)

def train(model, lattice, optim, y, rot, trans=None, ctf_params=None):
    model.train()
    optim.zero_grad()
    B = y.size(0)
    D = lattice.D
    # restrict to inscribed circle of pixels
    mask = lattice.get_circular_mask(D//2)
    yhat = model(lattice.coords[mask]/lattice.extent/2 @ rot).view(B,-1)
    if ctf_params is not None:
        freqs = lattice.freqs2d[mask]
        freqs = freqs.unsqueeze(0).expand(B, *freqs.shape)/ctf_params[:,0].view(B,1,1)
        yhat *= ctf.compute_ctf(freqs, *torch.split(ctf_params[:,1:], 1, 1))
    y = y.view(B,-1)[:, mask]
    if trans is not None:
        y = model.translate_ht(lattice.freqs2d[mask], y, trans.unsqueeze(1)).view(B,-1)
    loss = F.mse_loss(yhat, y)
    loss.backward()
    optim.step()
    return loss.item()

def main(args):
    log(args)
    t1 = dt.now()
    if not os.path.exists(args.outdir):
        os.makedirs(args.outdir)

    # set the random seed
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)

    ## set the device
    use_cuda = torch.cuda.is_available()
    device = torch.device('cuda' if use_cuda else 'cpu')
    log('Use cuda {}'.format(use_cuda))
    if use_cuda:
        torch.set_default_tensor_type(torch.cuda.FloatTensor)

    # load the particles
    if args.lazy:
        data = dataset.LazyMRCData(args.particles, norm=args.norm, invert_data=args.invert_data)
    else:
        data = dataset.MRCData(args.particles, norm=args.norm, invert_data=args.invert_data)
    D = data.D
    Nimg = data.N

    lattice = Lattice(D, extent=args.l_extent)
    model = FTPositionalDecoder(3, D, args.layers, args.dim, nn.ReLU)
    log(model)
    log('{} parameters in model'.format(sum(p.numel() for p in model.parameters() if p.requires_grad)))

    optim = torch.optim.Adam(model.parameters(), lr=args.lr, weight_decay=args.wd)

    if args.load:
        log('Loading model weights from {}'.format(args.load))
        checkpoint = torch.load(args.load)
        model.load_state_dict(checkpoint['model_state_dict'])
        optim.load_state_dict(checkpoint['optimizer_state_dict'])
        start_epoch = checkpoint['epoch']+1
        assert start_epoch < args.num_epochs
    else:
        start_epoch = 0

    rot = torch.tensor(utils.load_pkl(args.rot))
    if args.trans: trans = args.tscale*torch.tensor(utils.load_pkl(args.trans).astype(np.float32))

    if args.ctf is not None:
        log('Loading ctf params from {}'.format(args.ctf))
        ctf_params = utils.load_pkl(args.ctf)
        assert ctf_params.shape == (Nimg, 7)
        ctf.print_ctf_params(ctf_params[0])
        ctf_params = torch.tensor(ctf_params)
    else: ctf_params = None

    data_generator = DataLoader(data, batch_size=args.batch_size, shuffle=True)
    for epoch in range(start_epoch, args.num_epochs):
        loss_accum = 0
        batch_it = 0
        for batch, ind in data_generator:
            batch_it += len(ind)
            y = batch.to(device)
            r = rot[ind]
            t = trans[ind] if args.trans else None
            c = ctf_params[ind] if ctf_params is not None else None
            loss_item = train(model, lattice, optim, batch.to(device), r, t, c)
            loss_accum += loss_item*len(ind)
            if batch_it % args.log_interval == 0:
                log('# [Train Epoch: {}/{}] [{}/{} images] loss={:.4f}'.format(epoch+1, args.num_epochs, batch_it, Nimg, loss_item))
        log('# =====> Epoch: {} Average loss = {:.4}'.format(epoch+1, loss_accum/Nimg))
        if args.checkpoint and epoch % args.checkpoint == 0:
            out_mrc = '{}/reconstruct.{}.mrc'.format(args.outdir,epoch)
            out_weights = '{}/weights.{}.pkl'.format(args.outdir,epoch)
            save_checkpoint(model, lattice, optim, epoch, data.norm, out_mrc, out_weights)

    ## save model weights and evaluate the model on 3D lattice
    out_mrc = '{}/reconstruct.mrc'.format(args.outdir)
    out_weights = '{}/weights.pkl'.format(args.outdir)
    save_checkpoint(model, lattice, optim, epoch, data.norm, out_mrc, out_weights)
   
    td = dt.now()-t1
    log('Finsihed in {} ({} per epoch)'.format(td, td/(args.num_epochs-start_epoch)))

if __name__ == '__main__':
    args = parse_args().parse_args()
    utils._verbose = args.verbose
    main(args)
