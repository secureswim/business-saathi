/* Local CSS 3D mesh: a rounded, tapered housing behind a tilted QR panel. */
(() => {
  const model=document.getElementById('soundbox'),view=document.getElementById('model-view');
  let yaw=-28,pitch=-8,drag=null;
  const render=()=>model.style.transform=`rotateX(${pitch}deg) rotateY(${yaw}deg)`;
  view.addEventListener('pointerdown',e=>{if(e.target.closest('button')||e.button!==0)return;drag={x:e.clientX,y:e.clientY,yaw,pitch};view.setPointerCapture(e.pointerId);view.classList.add('dragging');});
  view.addEventListener('pointermove',e=>{if(!drag)return;yaw=drag.yaw+(e.clientX-drag.x)*.5;pitch=Math.max(-40,Math.min(15,drag.pitch-(e.clientY-drag.y)*.25));render();});
  const release=()=>{drag=null;view.classList.remove('dragging');};
  view.addEventListener('pointerup',release);view.addEventListener('pointercancel',release);
  document.getElementById('rotate').onclick=()=>{yaw+=45;render();};
  document.getElementById('rear-view').onclick=()=>{yaw=155;pitch=-8;render();};
  document.getElementById('reset-view').onclick=()=>{yaw=-28;pitch=-8;render();};
  // Each adjacent perimeter is joined by solid triangles. The local basis of
  // every face is orthonormal; scaling/skewing a 1px plane into a side wall
  // makes its transform numerically singular at near-edge-on angles.
  const rings=[
    {w:340,h:330,r:47,z:70,tilt:17,colour:[221,229,230]},
    {w:340,h:330,r:47,z:64,tilt:17,colour:[201,213,217]},
    {w:328,h:320,r:46,z:54,tilt:17,colour:[39,119,155]},
    {w:320,h:306,r:46,z:10,tilt:12,cy:6,colour:[31,111,151]},
    {w:300,h:290,r:43,z:-78,tilt:2,cy:12,colour:[27,101,143]},
    {w:286,h:286,r:40,z:-104,cy:12,colour:[24,88,129]},
  ];
  const mesh=document.getElementById('housing-mesh');
  const sub=(a,b)=>a.map((v,i)=>v-b[i]);
  const dot=(a,b)=>a.reduce((s,v,i)=>s+v*b[i],0);
  const cross=(a,b)=>[a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0]];
  const unit=a=>{const length=Math.hypot(...a);return a.map(v=>v/length);};
  const profile=({w,h,r,z,tilt=0,cy=0})=>{
    const points=[],angle=tilt*Math.PI/180;
    for(let corner=0;corner<4;corner++){
      const cx=[w/2-r,-w/2+r,-w/2+r,w/2-r][corner];
      const yy=[h/2-r,h/2-r,-h/2+r,-h/2+r][corner];
      for(let step=0;step<=10;step++){
        const a=(corner*90+step*9)*Math.PI/180;
        const x=cx+r*Math.cos(a),y=yy+r*Math.sin(a);
        points.push([x+170,y*Math.cos(angle)+cy+165,y*Math.sin(angle)+z]);
      }
    }
    return points;
  };
  const triangle=(a,b,c,colour)=>{
    const ab=sub(b,a),ac=sub(c,a),width=Math.hypot(...ab);
    if(width<.0001)return;
    const x=unit(ab),normal=unit(cross(ab,ac)),y=cross(normal,x);
    const cx=dot(ac,x),height=dot(ac,y);
    if(height<.0001)return;
    const minX=Math.min(0,cx),maxX=Math.max(width,cx),pad=.35;
    const origin=a.map((v,i)=>v+x[i]*(minX-pad)-y[i]*pad);
    const face=document.createElement('i');face.className='shell-surface';
    face.style.width=`${maxX-minX+2*pad}px`;face.style.height=`${height+2*pad}px`;
    // A small edge overlap hides rasterisation cracks between shared edges.
    face.style.clipPath=`polygon(${ -minX}px 0px,${width-minX+2*pad}px 0px,${cx-minX+pad}px ${height+2*pad}px)`;
    const light=.8+.12*normal[0]-.18*normal[1]+.08*normal[2];
    face.style.background=`rgb(${colour.map(v=>Math.round(v*light)).join(',')})`;
    face.style.transform=`matrix3d(${x.join(',')},0,${y.join(',')},0,${normal.join(',')},0,${origin.join(',')},1)`;
    mesh.appendChild(face);
  };
  const profiles=rings.map(profile);
  for(let section=0;section<profiles.length-1;section++){
    const a=profiles[section],b=profiles[section+1];
    for(let i=0;i<a.length;i++){
      const j=(i+1)%a.length;
      triangle(a[i],b[i],a[j],rings[section].colour);
      triangle(a[j],b[i],b[j],rings[section].colour);
    }
  }
  render();
  // Illustrative pattern only: no UPI address or payment payload.
  const rect=(x,y,w,h)=>`<rect x="${x}" y="${y}" width="${w}" height="${h}" fill="#161719"/>`;
  let svg='';
  for(let y=2;y<27;y++)for(let x=2;x<27;x++){if((x<10&&y<10)||(x>18&&y<10)||(x<10&&y>18))continue;if((x*31+y*17+x*y*7)%11<5)svg+=rect(x,y,1,1);}
  for(const [x,y] of [[2,2],[20,2],[2,20]])svg+=rect(x,y,7,7)+`<rect x="${x+1}" y="${y+1}" width="5" height="5" fill="white"/>`+rect(x+2,y+2,3,3);
  document.getElementById('demo-qr').innerHTML=svg;
})();
